"""Configuration management for Delegatron."""

from __future__ import annotations

import functools
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from delegatron.constraints import Constraints
from delegatron.utils.provider import is_local_base_url

logger = logging.getLogger(__name__)

# Load .env from the project root (where pyproject.toml lives) so that
# CLI commands, server, and direct imports all pick up the same env vars.
try:
    from dotenv import load_dotenv

    _project_env = Path(__file__).resolve().parents[2] / ".env"
    if _project_env.is_file():
        load_dotenv(_project_env, override=True)
except ImportError:
    pass


def _str_to_bool(value: str | bool) -> bool:
    """Convert string or bool to bool (for env vars and TOML)."""
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


# Supported provider types
PROVIDERS = ("anthropic", "openai", "google", "openai-compatible", "litellm")

# Default presets
PRESETS: dict[str, dict[str, dict[str, str | int | float]]] = {
    "default": {
        "architect": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "prompt_version": 1,
        },
        "implementer": {
            "provider": "openai",
            "model": "gpt-4.1",
            "prompt_version": 1,
        },
        "reviewer": {
            "provider": "google",
            "model": "gemini-2.5-pro",
            "prompt_version": 1,
        },
    },
    "claude-heavy": {
        "architect": {
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "prompt_version": 1,
        },
        "implementer": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "prompt_version": 1,
        },
        "reviewer": {
            "provider": "google",
            "model": "gemini-2.5-pro",
            "prompt_version": 1,
        },
    },
    "budget": {
        "architect": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "prompt_version": 1,
        },
        "implementer": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "prompt_version": 1,
        },
        "reviewer": {
            "provider": "google",
            "model": "gemini-2.5-flash",
            "prompt_version": 1,
        },
    },
    "mock": {
        "architect": {"provider": "mock", "model": "mock-architect"},
        "implementer": {"provider": "mock", "model": "mock-implementer"},
        "reviewer": {"provider": "mock", "model": "mock-reviewer"},
    },
    # ── Local presets (Ollama) ─────────────────────────────────────────
    # Phi-4 is the default architect + reviewer — empirically ~4x faster than
    # qwen3.5:9b with better review quality (79s vs 312s on code review).
    # Three variants differ only in the implementer model.
    #
    # VRAM guidance (12 GB class):
    #   "local-oss"        = gpt-oss:20b impl        — ★ BEST QUALITY (validated
    #                        2026-04-18; only local model to pass fib + React
    #                        in both impl and reviewer roles).  13 GB MXFP4.
    #   "local"            = qwen2.5-coder:14b impl  — stable baseline.
    #   "local-oss-review" = qwen2.5-coder impl +
    #                        gpt-oss:20b reviewer    — strongest local review
    #                        (~2x reviewer latency for tougher critique).
    #   "local-phi4"       = phi4:14b all roles      — fastest, simple tasks.
    #   "local-heavy"      = qwen3-coder:30b impl    — REQUIRES 24 GB+ VRAM.
    #                        Works on smaller cards but spills 30+ GB into
    #                        system RAM, 3x slower per token, and complex
    #                        multi-feature tasks exhaust the iteration budget.
    "local": {
        "architect": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        },
        "implementer": {
            "provider": "openai-compatible",
            "model": "qwen2.5-coder:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "reviewer": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "pipeline": {
            "max_iterations": 5,
            "max_cost_usd": 10.0,
            "max_wall_time_minutes": 60,
            "emcs_threshold": 0.6,
        },
    },
    # ── Local variant presets (benchmark different implementer models) ──
    "local-phi4": {
        "architect": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        },
        "implementer": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "reviewer": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "pipeline": {
            "max_iterations": 5,
            "max_cost_usd": 10.0,
            "max_wall_time_minutes": 60,
            "emcs_threshold": 0.6,
        },
    },
    "local-heavy": {
        "architect": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        },
        "implementer": {
            "provider": "openai-compatible",
            "model": "qwen3-coder:30b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "reviewer": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "pipeline": {
            "max_iterations": 10,
            "max_cost_usd": 10.0,
            "max_wall_time_minutes": 60,
            "emcs_threshold": 0.6,
        },
    },
    # "local-oss" — gpt-oss:20b as implementer.  Validated 2026-04-18:
    # only local candidate tested that passed both fib.py (Python) and
    # React Counter (TypeScript/Vitest) on the standard bar.  13 GB
    # MXFP4 — fits 12 GB VRAM with modest CPU offload.  Keep phi4 as
    # architect + reviewer (empirically ~4x faster than larger models).
    "local-oss": {
        "architect": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        },
        "implementer": {
            "provider": "openai-compatible",
            "model": "gpt-oss:20b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "reviewer": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "pipeline": {
            "max_iterations": 5,
            "max_cost_usd": 10.0,
            "max_wall_time_minutes": 60,
            "emcs_threshold": 0.6,
        },
    },
    # "local-oss-review" — qwen2.5-coder:14b implementer + gpt-oss:20b
    # reviewer.  Trades ~2x reviewer latency for stronger architectural
    # critique.  Validated 2026-04-18: approved fib + React cleanly as
    # reviewer.  Use when review rigor matters more than wallclock.
    "local-oss-review": {
        "architect": {
            "provider": "openai-compatible",
            "model": "phi4:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
        },
        "implementer": {
            "provider": "openai-compatible",
            "model": "qwen2.5-coder:14b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "reviewer": {
            "provider": "openai-compatible",
            "model": "gpt-oss:20b",
            "base_url": "http://localhost:11434/v1",
            "prompt_version": 5,
            "max_tokens": 16384,
        },
        "pipeline": {
            "max_iterations": 5,
            "max_cost_usd": 10.0,
            "max_wall_time_minutes": 60,
            "emcs_threshold": 0.6,
        },
    },
    # ── Cloud presets ────────────────────────────────────────────────
    "single-claude": {
        "architect": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "prompt_version": 4,
        },
        "implementer": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "prompt_version": 1,
        },
        "reviewer": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "prompt_version": 1,
        },
    },
    "single-gpt": {
        "architect": {
            "provider": "openai",
            "model": "gpt-4.1",
            "prompt_version": 4,
        },
        "implementer": {
            "provider": "openai",
            "model": "gpt-4.1",
            "prompt_version": 1,
        },
        "reviewer": {
            "provider": "openai",
            "model": "gpt-4.1",
            "prompt_version": 1,
        },
    },
}

# One-line description for each preset.  Surfaced by `delegatron list-presets`
# and by the dashboard preset picker via the /api/presets endpoint.
PRESET_DESCRIPTIONS: dict[str, str] = {
    "default": (
        "Cloud trio — Claude architect + GPT-4.1 implementer + Gemini reviewer. "
        "Best online quality."
    ),
    "claude-heavy": "Claude architect + implementer, Gemini reviewer.",
    "budget": "All Gemini Flash — cheapest online option.",
    "mock": "No API keys — mock providers for testing pipeline plumbing.",
    "local": "phi4 + qwen2.5-coder:14b + phi4 — 12 GB VRAM baseline, passes fib + React cleanly.",
    "local-phi4": "phi4:14b in all roles — fastest local option for simple tasks.",
    "local-heavy": (
        "phi4 + qwen3-coder:30b + phi4 — REQUIRES 24 GB+ VRAM "
        "(spills 30 GB to RAM on smaller cards)."
    ),
    "local-oss": (
        "phi4 + gpt-oss:20b + phi4 — best local quality (validated 2026-04-18: "
        "only local model to pass fib + React in both impl and reviewer roles)."
    ),
    "local-oss-review": (
        "phi4 + qwen2.5-coder:14b + gpt-oss:20b — strongest local review "
        "(trade ~2x reviewer latency for tougher architectural critique)."
    ),
    "single-claude": "All Claude Sonnet (single-model cloud baseline).",
    "single-gpt": "All GPT-4.1 (single-model cloud baseline).",
}


# Default API key env var per provider.  "openai-compatible" has none: its
# endpoints (Ollama, other vendors) must name their key with api_key_env.
DEFAULT_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "litellm": "OPENAI_API_KEY",
}

# The host a provider's default key belongs to; any other base_url host does not get it.
_PROVIDER_HOME_HOSTS: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "google": "generativelanguage.googleapis.com",
    "litellm": "api.openai.com",
}

# Providers that always call their vendor's cloud API, whatever the base_url.
_CLOUD_ONLY_PROVIDERS = frozenset({"anthropic", "google"})

SECURITY_MODES = ("allow", "warn", "block")

# Per-request timeout when a role does not set one (the provider SDKs' own
# default).  Local generations get longer: a non-streaming Ollama response
# arrives only once it is complete.
CLOUD_TIMEOUT_SECONDS = 600
LOCAL_TIMEOUT_SECONDS = 900

# Sampling temperature range accepted from config (the OpenAI/Gemini range;
# the Anthropic adapter narrows it further to its own [0, 1]).
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

# Sandboxes that isolate the code they run; entrypoint smoke runs default on there.
ISOLATED_SANDBOXES = frozenset({"docker", "e2b"})

# Comma-separated extra host names that count as local (zero-cloud report).
LOCAL_HOSTS_ENV = "DELEGATRON_LOCAL_HOSTS"


def _is_local_endpoint(provider: str, base_url: str) -> bool:
    """True for the mock provider and for OpenAI-style endpoints on this machine."""
    if provider == "mock":
        return True
    return provider not in _CLOUD_ONLY_PROVIDERS and is_local_base_url(base_url)


def clamp_temperature(value: Any, default: float = 0.0) -> float:
    """*value* as a float in ``[MIN_TEMPERATURE, MAX_TEMPERATURE]``; *default* if not a number."""
    temperature = _safe_float(value, default)
    if temperature != temperature:  # NaN
        temperature = default
    return min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, temperature))


@functools.cache
def _log_withheld_key(env_var: str, host: str, model: str = "") -> None:
    """Explain, once per endpoint and model, why a role is sent no API key."""
    logger.warning(
        "Not sending %s to %s%s: a provider's default key goes only to its own API host. "
        "Set api_key_env for this role to send a key there.",
        env_var,
        host,
        f" (the role running {model})" if model else "",
    )


@functools.cache
def _log_keyless_endpoint(provider: str, host: str) -> None:
    """Warn, once per endpoint, that a remote role has no API key configured."""
    logger.warning(
        "No API key for the %s endpoint %s: set api_key_env (or api_key) for this role "
        "if the endpoint needs one.",
        provider,
        host,
    )


@dataclass(frozen=True)
class RoleConfig:
    """Configuration for a single agent role (provider-agnostic)."""

    provider: str
    model: str
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    max_tokens: int = 16384
    temperature: float = 0.0
    prompt_version: int = 0  # 0 = auto: v1 for cloud endpoints, newest local set otherwise
    # Model context window in tokens; prompts are trimmed to fit. 0 = auto
    # (detected from local Ollama endpoints, unlimited elsewhere).
    context_window: int = 0
    timeout_seconds: int = CLOUD_TIMEOUT_SECONDS  # per provider request
    # False: never read a key from the environment (the server sets this when
    # an API caller points a role at its own base_url without its own key).
    allow_env_key: bool = True

    def resolve_api_key(self) -> str:
        """Resolve the API key: the explicit value, else an environment variable.

        ``api_key_env`` names the variable explicitly.  Otherwise the provider's
        default variable is used, but only when requests go to that provider's
        own API host, so a custom ``base_url`` never receives, say,
        ``OPENAI_API_KEY``.  With ``allow_env_key`` off, only ``api_key`` counts.
        """
        if self.api_key:
            return self.api_key
        if not self.allow_env_key:
            return ""
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        env_var = DEFAULT_API_KEY_ENV.get(self.provider, "")
        host = _url_host(self.base_url) if self.base_url else ""
        if not env_var:
            if self.base_url and self.provider != "mock" and not is_local_base_url(self.base_url):
                _log_keyless_endpoint(self.provider, host or self.base_url)
            return ""
        if self.base_url and host != _PROVIDER_HOME_HOSTS.get(self.provider):
            if not is_local_base_url(self.base_url):
                _log_withheld_key(env_var, host or self.base_url, self.model)
            return ""
        return os.environ.get(env_var, "")

    def __repr__(self) -> str:
        """Mask API key in repr to prevent accidental logging."""
        key = self.resolve_api_key()
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
        return f"RoleConfig(provider={self.provider!r}, model={self.model!r}, api_key={masked!r})"


def _url_host(url: str) -> str:
    """The lower-case host of *url* (scheme optional), or ``""``."""
    try:
        return urlparse(url if "://" in url else f"//{url}").hostname or ""
    except ValueError:
        return ""


def is_local_model(role: RoleConfig) -> bool:
    """True when *role* runs on this machine: the mock provider or a local endpoint.

    Locality comes from the provider and ``base_url``, never the model name
    (``gpt-oss:20b`` on Ollama is local; ``llama-3.3-70b`` on a cloud host is not).
    """
    return _is_local_endpoint(role.provider, role.base_url)


def normalize_security_mode(value: Any) -> str:
    """Return *value* as one of ``SECURITY_MODES`` (any case); raise ``ValueError`` otherwise."""
    mode = value.strip().lower() if isinstance(value, str) else ""
    if mode not in SECURITY_MODES:
        raise ValueError(f"security_mode must be one of {', '.join(SECURITY_MODES)}; got {value!r}")
    return mode


@dataclass(frozen=True)
class PipelineConfig:
    """Pipeline execution limits and settings."""

    max_iterations: int = 5
    max_cost_usd: float = 5.0
    max_wall_time_minutes: int = 60
    sandbox: str = "docker"
    docker_image: str = ""  # Custom Docker image (default: auto-detect or python:3.12-slim)
    log_level: str = "INFO"
    interactive: bool = False
    enable_arbitration: bool = True
    emcs_threshold: float = 0.8
    constraints: dict[str, Any] = field(default_factory=dict)
    security_mode: str = "warn"  # one of SECURITY_MODES
    parallel_tasks: bool = False  # Enable parallel task execution via git worktrees
    project_mode: bool = False  # Enable project mode (multi-milestone orchestration)
    # GitHub PR creation after successful runs. Requires `gh` CLI installed
    # and authenticated. Never causes a run to fail — PR errors log but pass.
    create_pr: bool = False
    pr_base: str = "main"  # Base branch for PRs
    pr_draft: bool = False  # Create PR as draft
    # Rerun failing pytest ids in the sandbox to flag flaky tests.
    flaky_rerun: bool = True
    # How often each still-failing test is rerun (capped by the sandbox at 5).
    flaky_reruns: int = 2
    # Count a test run as passed when every failing test passed on a rerun and
    # none of them is in a file this run wrote (TOML: tolerate_flaky_tests or
    # tolerate_preexisting_flaky).
    tolerate_flaky_tests: bool = False
    # Smoke-run script outputs after the tests.  None = on for isolated
    # sandboxes (docker, e2b), off for the local sandbox; see entrypoints_enabled.
    run_entrypoints: bool | None = None
    retry_temperature: float = 0.4  # Implementer temperature for retries that repeat
    # Run the workspace's tests once before the first patch and treat what
    # already fails as pre-existing.
    baseline_tests: bool = True
    # Extra host names (e.g. a LAN model server's FQDN) the zero-cloud report counts as local.
    local_hosts: tuple[str, ...] = ()
    # Re-plans a run may make before architecture issues go to the implementer.
    max_replans: int = 2
    # Run the repository's git hooks on Delegatron's own run commits.
    run_git_hooks: bool = False

    def entrypoints_enabled(self, sandbox: str | None = None) -> bool:
        """Whether entrypoint smoke runs happen in *sandbox* (default: ``self.sandbox``).

        An explicit ``run_entrypoints`` wins; unset, they run only in the
        isolated sandboxes, never in the local sandbox.
        """
        if self.run_entrypoints is not None:
            return self.run_entrypoints
        return (sandbox or self.sandbox).strip().lower() in ISOLATED_SANDBOXES


# Well-known Docker images for common platforms
DOCKER_IMAGES: dict[str, str] = {
    # Linux
    "python": "python:3.12-slim",
    "node": "node:22-slim",
    "go": "golang:1.23-bookworm",
    "rust": "rust:1.82-slim-bookworm",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:9.0",
    "java": "maven:3.9-eclipse-temurin-21",
    "ruby": "ruby:3.3-slim",
    # PHP projects use composer for deps; use the composer image which
    # bundles PHP + composer (php:*-cli does NOT include composer).
    "php": "composer:2",
    # Windows (requires Docker Desktop in Windows container mode)
    "windows": "mcr.microsoft.com/windows/servercore:ltsc2022",
    "windows-dotnet": "mcr.microsoft.com/dotnet/sdk:9.0-nanoserver-ltsc2022",
    "windows-python": "python:3.12-windowsservercore-ltsc2022",
}


@dataclass(frozen=True)
class SecurityConfig:
    """Settings for the per-patch security scanners (TOML ``[security]``)."""

    # Regexes for lines the secrets scanner must not report (e.g. test fixtures).
    secrets_allowlist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    """Top-level Delegatron configuration."""

    architect: RoleConfig = field(
        default_factory=lambda: RoleConfig(provider="anthropic", model="claude-sonnet-5-5")
    )
    implementer: RoleConfig = field(
        default_factory=lambda: RoleConfig(provider="openai", model="gpt-4.1")
    )
    reviewer: RoleConfig = field(
        default_factory=lambda: RoleConfig(provider="google", model="gemini-2.5-pro")
    )
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


ROLE_NAMES = ("architect", "implementer", "reviewer")

# Role keys tied to the endpoint.  A TOML role table that changes the preset's
# provider or base_url does not inherit them (a localhost URL must not follow
# a switch to a cloud provider, nor a cloud key a switch to another host).
_ENDPOINT_KEYS = frozenset({"base_url", "api_key", "api_key_env", "prompt_version"})


def _parse_role(data: dict[str, Any]) -> RoleConfig:
    """Parse a role config from a TOML dict."""
    provider = data.get("provider", "openai")
    base_url = data.get("base_url", "")
    default_timeout = (
        LOCAL_TIMEOUT_SECONDS if _is_local_endpoint(provider, base_url) else CLOUD_TIMEOUT_SECONDS
    )
    return RoleConfig(
        provider=provider,
        model=data.get("model", ""),
        api_key=data.get("api_key", ""),
        api_key_env=data.get("api_key_env", ""),
        base_url=base_url,
        max_tokens=data.get("max_tokens", 16384),
        temperature=clamp_temperature(data.get("temperature", 0.0)),
        prompt_version=data.get("prompt_version", 0),
        context_window=data.get("context_window", 0),
        timeout_seconds=_timeout_seconds(data.get("timeout_seconds"), default_timeout),
    )


def _timeout_seconds(value: Any, default: int) -> int:
    """A role's ``timeout_seconds``: a positive whole number, else *default* (logged).

    0 or a negative value would make every provider request time out at once.
    """
    if value is None:
        return default
    seconds = _safe_int(value, 0)
    if seconds <= 0:
        logger.warning(
            "timeout_seconds must be a positive number of seconds, got %r; using %d", value, default
        )
        return default
    return seconds


def _merge_role(preset_role: dict[str, Any], toml_role: dict[str, Any]) -> dict[str, Any]:
    """Overlay a TOML role table on the preset's role, key by key."""
    moved = any(
        key in toml_role and toml_role[key] != preset_role.get(key)
        for key in ("provider", "base_url")
    )
    inherited = {k: v for k, v in preset_role.items() if not (moved and k in _ENDPOINT_KEYS)}
    return {**inherited, **toml_role}


def _table(value: Any, name: str) -> dict[str, Any]:
    """Return a TOML table (missing = empty); anything else is a config error."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a table, got {type(value).__name__}")
    return value


def _implicit_toml(workspace: Path | None) -> Path:
    """``<workspace>/delegatron.toml`` when it exists, else ``./delegatron.toml``."""
    if workspace is not None and (workspace / "delegatron.toml").is_file():
        return workspace / "delegatron.toml"
    return Path("delegatron.toml")


def _parse_security(data: dict[str, Any]) -> SecurityConfig:
    """Parse the TOML ``[security]`` table."""
    allowlist = data.get("secrets_allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(p, str) for p in allowlist):
        raise ValueError("[security] secrets_allowlist must be a list of strings")
    return SecurityConfig(secrets_allowlist=list(allowlist))


def _safe_int(value: Any, default: int) -> int:
    """Parse an int, returning *default* if the value is not a valid integer."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float) -> float:
    """Parse a float, returning *default* if the value is not a valid number."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _optional_bool(value: Any) -> bool | None:
    """A TOML/env boolean, or ``None`` when unset (missing or empty)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _str_to_bool(value)


def _host_list(value: Any, name: str) -> tuple[str, ...]:
    """Host names from a TOML list or a comma-separated string."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list) and all(isinstance(h, str) for h in value):
        items = value
    else:
        raise ValueError(f"{name} must be a list of host names or a comma-separated string")
    return tuple(dict.fromkeys(h.strip().rstrip(".").lower() for h in items if h.strip()))


def load_config(
    config_path: Path | None = None,
    preset: str | None = None,
    *,
    workspace: Path | None = None,
) -> Config:
    """Load configuration from TOML file, preset, and/or environment variables.

    Priority: TOML file (*config_path*, else ``<workspace>/delegatron.toml``
    when it exists, else ``./delegatron.toml``) > preset > env vars >
    defaults.  A TOML role table is merged over the preset's role
    key by key; endpoint keys (``base_url``, keys, ``prompt_version``) are
    inherited only while the table keeps the preset's provider and base_url.
    Constraints come from ``[constraints]`` and ``[pipeline.constraints]``
    (the latter wins per key), scanner settings from ``[security]``.
    """
    # Start with defaults
    roles: dict[str, dict[str, Any]] = {}

    # Apply preset if specified
    if preset:
        if preset not in PRESETS:
            valid = ", ".join(sorted(PRESETS.keys()))
            msg = f"Unknown preset {preset!r}. Valid presets: {valid}"
            raise ValueError(msg)
        preset_data = PRESETS[preset]
        roles = {k: dict(v) for k, v in preset_data.items() if k != "pipeline"}
        # Preset-level pipeline overrides (e.g. local preset sets max_iterations)
        preset_pipeline = preset_data.get("pipeline", {})
    else:
        preset_pipeline = {}

    # Load TOML file if it exists
    toml_path = config_path or _implicit_toml(workspace)
    toml_data: dict[str, Any] = {}
    if toml_path.exists():
        try:
            raw = toml_path.read_bytes()
            # Strip UTF-8 BOM if present (common in Windows editors)
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            toml_data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid TOML in {toml_path}: {e}") from e
        logger.info("Loaded configuration from %s", toml_path.resolve())

    toml_roles = _table(toml_data.get("roles"), "roles")
    overridden = [name for name in ROLE_NAMES if name in toml_roles]
    for role_name in overridden:
        roles[role_name] = _merge_role(
            roles.get(role_name, {}), _table(toml_roles[role_name], f"roles.{role_name}")
        )
    if preset and overridden and config_path is None:
        logger.warning(
            "%s (found implicitly) overrides the %s role(s) of preset %r",
            toml_path.resolve(),
            ", ".join(overridden),
            preset,
        )

    pipeline_data = _table(toml_data.get("pipeline"), "pipeline")
    constraints = {
        **_table(toml_data.get("constraints"), "constraints"),
        **_table(pipeline_data.get("constraints"), "pipeline.constraints"),
    }
    if constraints:
        # Malformed entries (e.g. an invalid ``re:`` regex) fail here, not mid-run.
        Constraints.from_dict(constraints)

    # Merge: preset pipeline defaults < TOML overrides
    merged_pipeline = {**preset_pipeline, **pipeline_data}

    # Build role configs (fall back to env vars for models)
    architect_data = roles.get("architect", {})
    architect_data.setdefault("provider", "anthropic")
    architect_data.setdefault(
        "model", os.environ.get("DELEGATRON_ARCHITECT_MODEL", "claude-sonnet-5-5")
    )

    implementer_data = roles.get("implementer", {})
    implementer_data.setdefault("provider", "openai")
    implementer_data.setdefault("model", os.environ.get("DELEGATRON_IMPLEMENTER_MODEL", "gpt-4.1"))

    reviewer_data = roles.get("reviewer", {})
    reviewer_data.setdefault("provider", "google")
    reviewer_data.setdefault("model", os.environ.get("DELEGATRON_REVIEWER_MODEL", "gemini-2.5-pro"))

    return Config(
        architect=_parse_role(architect_data),
        implementer=_parse_role(implementer_data),
        reviewer=_parse_role(reviewer_data),
        pipeline=PipelineConfig(
            max_iterations=max(
                1,
                _safe_int(
                    merged_pipeline.get(
                        "max_iterations", os.environ.get("DELEGATRON_MAX_ITERATIONS", "5")
                    ),
                    5,
                ),
            ),
            # 0 means no cost limit (local runs accrue estimated electricity cost).
            max_cost_usd=max(
                0.0,
                _safe_float(
                    merged_pipeline.get(
                        "max_cost_usd",
                        os.environ.get("DELEGATRON_MAX_COST_USD", "5.0"),
                    ),
                    5.0,
                ),
            ),
            max_wall_time_minutes=max(
                1,
                _safe_int(
                    merged_pipeline.get(
                        "max_wall_time_minutes", os.environ.get("DELEGATRON_MAX_WALL_TIME", "60")
                    ),
                    30,
                ),
            ),
            sandbox=merged_pipeline.get("sandbox", os.environ.get("DELEGATRON_SANDBOX", "docker")),
            docker_image=merged_pipeline.get(
                "docker_image", os.environ.get("DELEGATRON_DOCKER_IMAGE", "")
            ),
            log_level=merged_pipeline.get(
                "log_level", os.environ.get("DELEGATRON_LOG_LEVEL", "INFO")
            ),
            interactive=_str_to_bool(merged_pipeline.get("interactive", False)),
            enable_arbitration=_str_to_bool(
                merged_pipeline.get("enable_arbitration", True),
            ),
            emcs_threshold=float(
                merged_pipeline.get("emcs_threshold", 0.8),
            ),
            constraints=constraints,
            security_mode=normalize_security_mode(merged_pipeline.get("security_mode", "warn")),
            parallel_tasks=_str_to_bool(
                merged_pipeline.get(
                    "parallel_tasks",
                    os.environ.get("DELEGATRON_PARALLEL_TASKS", "false"),
                )
            ),
            project_mode=_str_to_bool(
                merged_pipeline.get(
                    "project_mode",
                    os.environ.get("DELEGATRON_PROJECT_MODE", "false"),
                )
            ),
            create_pr=_str_to_bool(
                merged_pipeline.get(
                    "create_pr",
                    os.environ.get("DELEGATRON_CREATE_PR", "false"),
                )
            ),
            pr_base=merged_pipeline.get("pr_base", os.environ.get("DELEGATRON_PR_BASE", "main")),
            pr_draft=_str_to_bool(
                merged_pipeline.get(
                    "pr_draft",
                    os.environ.get("DELEGATRON_PR_DRAFT", "false"),
                )
            ),
            flaky_rerun=_str_to_bool(merged_pipeline.get("flaky_rerun", True)),
            flaky_reruns=max(0, min(_safe_int(merged_pipeline.get("flaky_reruns", 2), 2), 5)),
            tolerate_flaky_tests=_str_to_bool(
                merged_pipeline.get(
                    "tolerate_flaky_tests",
                    merged_pipeline.get("tolerate_preexisting_flaky", False),
                )
            ),
            run_entrypoints=_optional_bool(merged_pipeline.get("run_entrypoints")),
            retry_temperature=clamp_temperature(merged_pipeline.get("retry_temperature", 0.4), 0.4),
            baseline_tests=_str_to_bool(
                merged_pipeline.get(
                    "baseline_tests", os.environ.get("DELEGATRON_BASELINE_TESTS", "true")
                )
            ),
            local_hosts=_host_list(
                merged_pipeline.get("local_hosts", os.environ.get(LOCAL_HOSTS_ENV)),
                "pipeline.local_hosts",
            ),
            max_replans=max(0, _safe_int(merged_pipeline.get("max_replans", 2), 2)),
            run_git_hooks=_str_to_bool(merged_pipeline.get("run_git_hooks", False)),
        ),
        security=_parse_security(_table(toml_data.get("security"), "security")),
    )
