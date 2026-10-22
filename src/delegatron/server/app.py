"""FastAPI server for Delegatron — REST API + SSE streaming.

Endpoints:
  POST /api/runs          — Start a new pipeline run
  GET  /api/runs          — List all runs
  GET  /api/runs/{id}     — Get run summary
  GET  /api/runs/{id}/events — SSE stream of live events
  POST /api/benchmark     — Start a benchmark comparison run
  GET  /api/benchmark/{id} — Get benchmark status/results
  POST /api/webhooks/github — GitHub webhook handler
  GET  /api/health        — Health check
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

# Load .env file into environment before anything else reads os.environ.
# Search project root (where pyproject.toml lives) in addition to cwd.
try:
    from dotenv import load_dotenv

    _project_env = Path(__file__).resolve().parents[3] / ".env"
    if _project_env.is_file():
        load_dotenv(_project_env, override=True)
    else:
        load_dotenv(override=True)
except ImportError:
    pass  # python-dotenv not installed — rely on actual env vars
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from delegatron import __version__
from delegatron.artifacts import ArtifactStore
from delegatron.config import (
    CLOUD_TIMEOUT_SECONDS,
    DEFAULT_API_KEY_ENV,
    LOCAL_TIMEOUT_SECONDS,
    Config,
    RoleConfig,
    is_local_model,
    load_config,
)
from delegatron.constraints import Constraints, merge_constraints
from delegatron.multirepo import BRANCH_PATTERN, RepoSyncError
from delegatron.repo_resolve import git_url_host, is_git_url, resolve_workspace
from delegatron.server.auth import APIKeyManager, AuthUser
from delegatron.server.db import init_db
from delegatron.server.run_workspace import (
    RUNS_DIR_NAME,
    collect_run_files,
    init_run_workspace,
    iteration_sha,
    materialize_diff,
    recorded_run_workspace,
    remove_tree,
    run_file_paths,
)
from delegatron.server.teams import TeamStore
from delegatron.templates import RunTemplate, TemplateStore
from delegatron.utils.cost import logged_cost_usd
from delegatron.utils.logging import setup_server_logging
from delegatron.utils.paths import REVIEWS_DIR_NAME, is_internal_path
from delegatron.workspace import Workspace, WorkspaceInitError

logger = logging.getLogger(__name__)

# Set up rolling file logs for the server
setup_server_logging(level=os.environ.get("DELEGATRON_LOG_LEVEL", "INFO"))


def _get_log_timezone() -> str:
    """Return current log timezone setting."""
    try:
        import delegatron.utils.logging as log_mod

        return getattr(log_mod, "LOG_TIMEZONE", "utc")
    except Exception:
        return "utc"


def _invalidate_runs_cache() -> None:
    """Clear the runs cache so the next request re-scans."""
    global _runs_cache, _runs_cache_time
    _runs_cache = None
    _runs_cache_time = 0.0


def _get_cached_runs(team_id: str | None = None) -> list[dict[str, Any]]:
    """Return all run summaries, using cache if fresh."""
    import time as _time

    global _runs_cache, _runs_cache_time

    now = _time.monotonic()
    if _runs_cache is not None and (now - _runs_cache_time) < _CACHE_TTL_SECONDS:
        runs = _runs_cache
    else:
        runs = []
        if _artifacts_base.exists():
            for d in sorted(_artifacts_base.iterdir()):
                if not d.is_dir():
                    continue
                summary_path = d / "summary.json"
                if summary_path.exists():
                    summary = _safe_load_json(summary_path)
                    if summary is not None:
                        summary = _live_status(d, _repair_restored_summary(d, summary))
                        if "run_id" not in summary and "review_id" in summary:
                            continue
                        summary.setdefault("run_id", d.name)
                        # Attach metadata from meta.json if available
                        meta_path = d / "meta.json"
                        if meta_path.exists():
                            meta = _safe_load_json(meta_path)
                            if meta:
                                summary.setdefault(
                                    "_team_id",
                                    meta.get("team_id"),
                                )
                                summary.setdefault(
                                    "_preset",
                                    meta.get("preset"),
                                )
                                # Fallback for cancelled/interrupted runs
                                # where summary.json may lack the request
                                if meta.get("request"):
                                    summary.setdefault(
                                        "request",
                                        meta["request"],
                                    )
                                if meta.get("created_at"):
                                    summary.setdefault(
                                        "created_at",
                                        meta["created_at"],
                                    )
                        # Fallback: use summary.json mtime
                        if "created_at" not in summary:
                            mtime = summary_path.stat().st_mtime
                            summary["created_at"] = datetime.fromtimestamp(
                                mtime,
                                tz=UTC,
                            ).isoformat()
                        runs.append(summary)
                elif d.name in _run_tasks:
                    entry: dict[str, Any] = {
                        "run_id": d.name,
                        "status": "running",
                    }
                    meta_path = d / "meta.json"
                    if meta_path.exists():
                        meta = _safe_load_json(meta_path)
                        if meta:
                            if meta.get("request"):
                                entry["request"] = meta["request"]
                            if meta.get("created_at"):
                                entry["created_at"] = meta["created_at"]
                            entry["_team_id"] = meta.get("team_id")
                            entry["_preset"] = meta.get("preset")
                    runs.append(entry)
        _runs_cache = runs
        _runs_cache_time = now

    # Filter by team if auth is enabled
    if team_id is not None:
        return [r for r in runs if r.get("_team_id") == team_id]
    return list(runs)


def _ensure_db_initialized() -> None:
    """Lazily initialize the database and stores on first use.

    This avoids filesystem side-effects at import time while still
    allowing the module to work without an explicit startup call.
    """
    global api_key_manager, team_store
    if api_key_manager is not None:
        return
    init_db(_db_path)
    api_key_manager = APIKeyManager(db_path=_db_path)
    team_store = TeamStore(db_path=_db_path)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: init DB + stores on startup, cleanup on shutdown."""
    _configure_from_env()
    _ensure_db_initialized()
    yield
    if _background_tasks:
        logger.info("Shutting down: cancelling %d background task(s)", len(_background_tasks))
        for task in list(_background_tasks):
            task.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        _background_tasks.clear()
    _active_streams.clear()


app = FastAPI(
    title="Delegatron API",
    version=__version__,
    description="Autonomous multi-agent coding pipeline API",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "DELEGATRON_CORS_ORIGINS",
        # Allow dashboard on common port offsets (port+1) for any server port.
        "http://localhost:3000,http://localhost:8001,http://localhost:8002,http://localhost:8081",
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# NOTE: The following in-memory stores (_active_streams, _background_tasks,
# _pending_approvals) are NOT safe for multi-process deployments.  This server
# MUST be run as a single process (e.g. ``uvicorn --workers 1``).  If
# horizontal scaling is required, replace these with a shared backend such as
# Redis or a database-backed queue.

# In-memory store for active run streams
# Maps run_id -> list of asyncio.Queues (one per subscriber)
_active_streams: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}

# Run summary cache — avoids re-scanning directories on every request
_runs_cache: list[dict[str, Any]] | None = None
_runs_cache_time: float = 0.0
_CACHE_TTL_SECONDS = 2.0  # Short TTL — fresh enough for dashboards

# Background task references (prevent GC)
_background_tasks: set[asyncio.Task[None]] = set()

# Maps run_id -> asyncio.Task for cancellation
_run_tasks: dict[str, asyncio.Task[None]] = {}

# Resumed runs whose new session has not finished yet.  Their summary.json
# still holds the previous session's outcome, so listings report them as
# "running" until the session writes its own.
_resumed_live: set[str] = set()

# GPU concurrency: only one local-model run at a time.
# Cloud-only runs could bypass this, but for simplicity we serialize all runs.
_run_semaphore = asyncio.Semaphore(1)

# Default workspace (overridden at startup by configure())
_workspace_root = Path(".")
_artifacts_base = Path("runs")
_configured = False

# POST /api/review stores its reports in <workspace root>/reviews/review-<id>
# (``REVIEWS_DIR_NAME``, one of the workspace's internal directories).


def _reviews_dir() -> Path:
    """The server's review store; never copied into run workspaces."""
    return _workspace_root / REVIEWS_DIR_NAME


# Interactive plan approval: stores pending plans waiting for user decision
# Key: run_id, Value: {"plan": dict, "event": asyncio.Event, "approved": bool}
_pending_approvals: dict[str, dict[str, Any]] = {}

# Webhook secret for GitHub signature verification
_webhook_secret: str | None = os.environ.get("DELEGATRON_WEBHOOK_SECRET")

# --- Auth / team stores ---
# Stores are properly initialized during application lifespan (_lifespan).
# Module-level placeholders are set to ``None`` so the module can be imported
# without triggering filesystem side-effects.  The lifespan handler creates
# the real instances before any request is served.

_data_dir = Path(os.environ.get("DELEGATRON_DATA_DIR", "data"))
_db_path = _data_dir / "delegatron.db"

api_key_manager: APIKeyManager = None  # type: ignore[assignment]
team_store: TeamStore = None  # type: ignore[assignment]

# Eagerly initialize so tests using TestClient (no lifespan) still work.
_ensure_db_initialized()

_DEFAULT_USER = AuthUser(
    user_id="default",
    email="default@localhost",
    team_id="default",
    role="admin",
)

_template_store = TemplateStore(_data_dir / "templates")


def _auth_enabled() -> bool:
    """Check whether authentication is enabled via env var."""
    return os.environ.get("DELEGATRON_AUTH_ENABLED", "false").lower() in ("true", "1", "yes")


# --- Short-lived signed URLs for SSE/download ---
# These replace raw token query params to prevent credential leakage
# in browser history, proxy logs, and audit trails.
#
# The signed token embeds the authenticated identity (user_id, team_id,
# role) directly in the HMAC payload. Verification reconstructs the
# AuthUser without needing the raw API key.

_STREAM_TOKEN_SECRET: str | None = os.environ.get("DELEGATRON_STREAM_SECRET")
_STREAM_TOKEN_TTL = 300  # 5 minutes

# Fail fast: when auth is enabled, a stable stream secret is REQUIRED.
# A per-process random secret breaks across restarts/workers.
if _auth_enabled() and not _STREAM_TOKEN_SECRET:
    raise RuntimeError(
        "DELEGATRON_STREAM_SECRET must be set when DELEGATRON_AUTH_ENABLED=true. "
        'Generate one with: python -c "import secrets; print(secrets.token_hex(32))" '
        "and set it in your environment. Signed stream URLs for SSE/download "
        "will not work without a stable secret."
    )

# When auth is disabled, a random secret is fine (dev mode only)
if not _STREAM_TOKEN_SECRET:
    _STREAM_TOKEN_SECRET = uuid.uuid4().hex


def _sign_stream_url(path: str, user: AuthUser) -> tuple[str, str]:
    """Create a short-lived signed URL for SSE/download endpoints.

    The HMAC payload includes the user identity so verification can
    reconstruct an AuthUser without needing the raw API key.

    Returns (signature, expiry_timestamp).
    """
    import time

    expires = str(int(time.time()) + _STREAM_TOKEN_TTL)
    # Encode identity into the signed payload
    payload = f"{path}:{expires}:{user.user_id}:{user.team_id}:{user.role}"
    assert _STREAM_TOKEN_SECRET is not None
    sig = hmac.new(
        _STREAM_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return sig, expires


def _verify_signed_url(request: Request) -> AuthUser:
    """Verify a signed stream URL and return the authenticated user.

    Reconstructs the AuthUser from the signed payload — no raw key
    lookup needed.

    Raises HTTPException on invalid/expired signatures.
    """
    import time

    path = request.url.path
    sig = request.query_params.get("sig", "").strip()
    expires = request.query_params.get("expires", "").strip()
    uid = request.query_params.get("uid", "").strip()
    tid = request.query_params.get("tid", "").strip()
    role = request.query_params.get("role", "").strip()

    if not all([sig, expires, uid, tid, role]):
        raise HTTPException(401, "Missing signed URL parameters")

    # Check expiry
    try:
        exp_ts = int(expires)
    except ValueError as err:
        raise HTTPException(401, "Invalid expires timestamp") from err

    if time.time() > exp_ts:
        raise HTTPException(401, "Signed URL expired")

    # Verify HMAC
    assert _STREAM_TOKEN_SECRET is not None
    payload = f"{path}:{expires}:{uid}:{tid}:{role}"
    expected = hmac.new(
        _STREAM_TOKEN_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "Invalid signature")

    return AuthUser(user_id=uid, email="", team_id=tid, role=role)


# Loud startup warning when auth is disabled
if not _auth_enabled():
    logger.warning(
        "*** INSECURE DEV MODE: DELEGATRON_AUTH_ENABLED is not set. "
        "All endpoints are unauthenticated. DO NOT deploy publicly without "
        "setting DELEGATRON_AUTH_ENABLED=true ***"
    )

# Inform about LLM provider configuration
_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
if not any(os.environ.get(k) for k in _PROVIDER_KEYS):
    logger.info(
        "No cloud LLM API keys set (%s). "
        "Cloud presets will be unavailable. "
        "Local presets (ollama-*) work without API keys.",
        ", ".join(_PROVIDER_KEYS),
    )


async def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency: extract and validate Bearer token.

    When ``DELEGATRON_AUTH_ENABLED`` is *not* set (or ``"false"``), a default
    admin user is returned so existing deployments keep working.
    """
    _ensure_db_initialized()
    if not _auth_enabled():
        return _DEFAULT_USER

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
    else:
        # For SSE/download: use short-lived signed URLs (EventSource
        # can't set headers). The signed URL carries the identity
        # directly — no raw API key in the URL.
        path = request.url.path
        if "/events" in path or "/download" in path:
            sig = request.query_params.get("sig", "").strip()
            if sig:
                # Signed URL path — returns AuthUser directly
                return _verify_signed_url(request)
            else:
                token = ""
        else:
            token = ""

    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing Authorization header. For SSE/download, "
                "use signed URLs via GET /api/stream-token."
            ),
        )

    api_key = api_key_manager.validate_key(token)
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    # Look up team membership to build AuthUser
    team = team_store.get_team(api_key.team_id)
    email = ""
    role = "member"
    if team is not None:
        for member in team.members:
            if member.user_id == api_key.user_id:
                email = member.email
                role = member.role
                break

    return AuthUser(
        user_id=api_key.user_id,
        email=email,
        team_id=api_key.team_id,
        role=role,
    )


# The directory where Delegatron itself is installed — must never be used as a workspace.
_install_dir = Path(__file__).resolve().parent.parent.parent.parent


def _is_install_dir(path: Path) -> bool:
    """Check if *path* is the Delegatron installation directory or its source tree.

    Blocks: the install dir itself, src/, tests/, dashboard/ — i.e. Delegatron's
    own code.  Allows: dedicated workspace subdirectories like chess-workspace/
    that live under the install dir but are separate projects.
    """
    try:
        resolved = path.resolve()
        if resolved == _install_dir:
            return True
        # Block Delegatron's own source/test/dashboard trees
        protected = (_install_dir / "src", _install_dir / "tests", _install_dir / "dashboard")
        return any(resolved == p or resolved.is_relative_to(p) for p in protected)
    except (OSError, ValueError):
        return False


def configure(workspace: Path, artifacts: Path | None = None) -> None:
    """Configure server paths (called before startup).

    Artifacts default to ``DELEGATRON_ARTIFACTS_DIR`` when set, else the
    install directory's ``runs/``.
    """
    global _workspace_root, _artifacts_base, _configured
    if _is_install_dir(workspace):
        logger.warning(
            "Workspace points to Delegatron installation directory (%s). "
            "Using default workspace instead to avoid polluting the installation.",
            workspace,
        )
        workspace = Path.home() / "delegatron-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        logger.info("Default workspace: %s", workspace)
    artifacts_env = os.environ.get("DELEGATRON_ARTIFACTS_DIR")
    _workspace_root = workspace
    _artifacts_base = artifacts or (Path(artifacts_env) if artifacts_env else _install_dir / "runs")
    _configured = True
    _invalidate_runs_cache()


def _configure_from_env() -> None:
    """Configure from ``DELEGATRON_WORKSPACE`` when nothing called :func:`configure`.

    Serving the app directly (``uvicorn delegatron.server.app:app``, as the
    Docker image does) skips ``delegatron serve``; this applies the same
    configuration from the environment. No-op when the variable is unset or
    the server is already configured.
    """
    workspace = os.environ.get("DELEGATRON_WORKSPACE")
    if workspace and not _configured:
        configure(Path(workspace).resolve())


# --- Request validation helpers ---

_DEFAULT_GIT_HOSTS = "github.com,gitlab.com,bitbucket.org"


def _env_list(name: str, default: str = "") -> list[str]:
    """Read a comma-separated operator setting as a list of non-empty items."""
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _validate_workspace_value(value: str | None) -> str | None:
    """Accept an existing local directory or a git URL on an allowed host.

    Local paths must lie under ``DELEGATRON_WORKSPACE_ROOT`` when it is set.
    Git URLs must be https, ssh or ``user@host:path`` on a host listed in
    ``DELEGATRON_GIT_ALLOWED_HOSTS`` (default: github.com, gitlab.com,
    bitbucket.org). Option-like values are rejected because a workspace
    string can reach ``git clone``. An empty value means the server default.
    """
    if not value:
        return None
    if value.startswith("-"):
        raise ValueError("Workspace must not start with '-'")
    path = Path(value)
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"Workspace path is not a directory: {value}")
        allowed_root_env = os.environ.get("DELEGATRON_WORKSPACE_ROOT")
        if allowed_root_env:
            allowed_root = Path(allowed_root_env).resolve()
            if not path.resolve().is_relative_to(allowed_root):
                raise ValueError(f"Workspace must be under {allowed_root}")
        return value
    if not is_git_url(value):
        raise ValueError(
            f"Workspace is neither an existing directory nor a supported git URL: {value}"
        )
    allowed_hosts = {
        h.lower() for h in _env_list("DELEGATRON_GIT_ALLOWED_HOSTS", _DEFAULT_GIT_HOSTS)
    }
    if git_url_host(value) not in allowed_hosts:
        raise ValueError("Git host is not allowed (DELEGATRON_GIT_ALLOWED_HOSTS)")
    return value


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic errors in FastAPI's 422 ``detail`` shape, without echoing input."""
    return cast(
        list[dict[str, Any]],
        exc.errors(include_url=False, include_context=False, include_input=False),
    )


# --- Request/Response models ---


class RoleOverride(BaseModel):  # type: ignore[misc, unused-ignore]
    """Optional per-role overrides (provider, model, base_url, etc.)."""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # 0 = resolve by endpoint (cloud / local set); unset keeps the configured
    # value, or re-resolves it when the role moves to another endpoint.
    prompt_version: int | None = Field(default=None, ge=0)
    # Unset keeps the configured timeout, or the default of the role's new
    # endpoint (cloud or local) when the role moves.
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200)


class ConstraintsConfig(BaseModel):  # type: ignore[misc, unused-ignore]
    """Project-level constraints for the pipeline.

    Pattern entries are literal substrings unless prefixed with ``re:``; they
    may be a list or a ``{name: pattern}`` table, as in ``delegatron.toml``.
    ``guidelines`` are prose rules for the agents and are never pattern-checked.
    """

    no_modify: list[str] = Field(default_factory=list)
    required_patterns: list[str] | dict[str, str] = Field(default_factory=list)
    forbidden_patterns: list[str] | dict[str, str] = Field(default_factory=list)
    guidelines: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_patterns(self) -> ConstraintsConfig:
        # Parse with the pipeline's own parser so an invalid ``re:`` entry is
        # rejected here instead of being skipped during the run.
        Constraints.from_dict(self.model_dump())
        return self


class RunRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request to start a pipeline run."""

    request: str = Field(..., min_length=1, max_length=100_000)
    preset: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    config_path: str | None = None
    workspace: str | None = None
    # Branch to clone when ``workspace`` is a git URL (default: the remote's default branch).
    branch: str | None = Field(default=None, max_length=255, pattern=BRANCH_PATTERN)
    sandbox: str | None = Field(default=None, pattern=r"^(docker|e2b)$")
    docker_image: str | None = Field(
        default=None,
        pattern=(
            r"^[a-zA-Z0-9._\-/]+"
            r"(:[0-9]+/[a-zA-Z0-9._\-/]+"
            r"(:[a-zA-Z0-9._\-]+)?|:[a-zA-Z0-9._\-]+)?$"
        ),
    )
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    max_cost_usd: float | None = Field(default=None, ge=0, le=1000)
    max_wall_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    interactive: bool | None = None

    # GitHub PR creation (requires `gh` CLI authenticated; only opens a
    # PR when the run finishes with status="approved"; never fails the
    # run if PR creation fails).  See CLI flags of the same names.
    create_pr: bool | None = None
    pr_base: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._\-/]+$")
    pr_draft: bool | None = None

    # Per-role overrides (applied on top of preset)
    architect: RoleOverride | None = None
    implementer: RoleOverride | None = None
    reviewer: RoleOverride | None = None

    # Constraints
    constraints: ConstraintsConfig | None = None

    @field_validator("config_path")
    @classmethod
    def validate_config_path(cls, v: str | None) -> str | None:
        if v is not None and (".." in v or v.startswith("/")):
            msg = "config_path must be a relative path without '..'"
            raise ValueError(msg)
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str | None) -> str | None:
        return _validate_workspace_value(v)


class RunResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Response after starting a run."""

    run_id: str
    status: str
    message: str


class RetryRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Optional body for the /retry endpoint."""

    fresh_start: bool = False
    preset: str | None = None
    implementer: RoleOverride | None = None
    architect: RoleOverride | None = None
    reviewer: RoleOverride | None = None


class ResumeRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Optional body for /resume: new budget limits for the continued run."""

    max_iterations: int | None = Field(default=None, ge=1, le=100)
    max_wall_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_cost_usd: float | None = Field(default=None, ge=0, le=1000)


class RunSummary(BaseModel):  # type: ignore[misc, unused-ignore]
    """Summary of a completed run."""

    run_id: str
    status: str
    iterations: int = 0
    total_cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    request: str = ""
    plan_summary: str = ""
    is_approved: bool = False
    error: str = ""


class TemplateRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request to create or update a template."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    request_pattern: str = ""
    preset: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, str] = Field(default_factory=dict)


class TemplateRunRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request to run a template with optional variable overrides."""

    variables: dict[str, str] = Field(default_factory=dict)
    preset: str | None = None
    workspace: str | None = None
    sandbox: str | None = None


# --- Endpoints ---


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


class StreamTokenResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Response from /api/stream-token."""

    url: str
    expires_in_seconds: int


@app.get("/api/stream-token", response_model=StreamTokenResponse)
async def get_stream_token(
    path: str,
    user: AuthUser = Depends(get_current_user),
) -> StreamTokenResponse:
    """Get a short-lived signed URL for SSE/download endpoints.

    The client calls this with a valid Bearer token and the target path,
    and receives a signed URL that expires in 5 minutes. This avoids
    putting long-lived API keys in query parameters.

    Usage: GET /api/stream-token?path=/api/runs/abc/events
    Returns: {"url": "/api/runs/abc/events?sig=...&expires=...&uid=...&tid=...&role=..."}
    """
    if not path.startswith("/api/runs/"):
        raise HTTPException(400, "Only /api/runs/* paths can be signed")
    sig, expires = _sign_stream_url(path, user)
    signed_url = (
        f"{path}?sig={sig}&expires={expires}&uid={user.user_id}&tid={user.team_id}&role={user.role}"
    )
    return StreamTokenResponse(url=signed_url, expires_in_seconds=_STREAM_TOKEN_TTL)


@app.get("/api/health/full")
async def health_full() -> dict[str, Any]:
    """Full system health check — server, Ollama, Docker, disk, active runs."""
    import shutil

    # Ollama
    ollama = await _check_ollama()

    # Docker (run in thread to avoid blocking event loop)
    docker_ok = False
    docker_version = ""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        docker_ok = result.returncode == 0
        docker_version = result.stdout.strip() if docker_ok else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Disk
    try:
        disk_path = str(_artifacts_base) if _artifacts_base.exists() else str(Path.cwd())
        disk = shutil.disk_usage(disk_path)
        disk_free_gb = round(disk.free / (1024**3), 1)
        disk_total_gb = round(disk.total / (1024**3), 1)
        disk_used_pct = round((disk.used / disk.total) * 100, 1)
    except OSError:
        disk_free_gb = disk_total_gb = disk_used_pct = 0.0

    # Active runs
    active_count = len(_active_streams)

    # Provider keys
    providers = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
    }

    return {
        "status": "ok",
        "version": __version__,
        "server": {
            "uptime_seconds": None,  # Could track with start_time
            "active_runs": active_count,
            "auth_enabled": _auth_enabled(),
        },
        "ollama": ollama,
        "docker": {
            "available": docker_ok,
            "version": docker_version,
        },
        "disk": {
            "free_gb": disk_free_gb,
            "total_gb": disk_total_gb,
            "used_percent": disk_used_pct,
        },
        "providers": providers,
    }


@app.get("/api/provider-keys")
async def provider_key_status(
    user: AuthUser = Depends(get_current_user),
) -> dict[str, bool]:
    """Return which LLM provider API keys are configured (set, not values)."""
    return {
        "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
        "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
    }


@app.post("/api/ollama/restart")
async def restart_ollama(
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Restart the Ollama server. Useful when local models hit stuck state.

    On Windows: stops ollama.exe and starts `ollama serve` fresh.
    On Unix: sends SIGTERM and restarts.

    After restart, all loaded models are unloaded and KV caches are cleared.
    """
    if _auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    import platform

    import httpx

    try:
        # Unload all models first (graceful)
        async with httpx.AsyncClient(timeout=5.0) as client:
            with contextlib.suppress(Exception):
                r = await client.get("http://localhost:11434/api/tags")
                if r.status_code == 200:
                    for model in r.json().get("models", []):
                        with contextlib.suppress(Exception):
                            await client.post(
                                "http://localhost:11434/api/generate",
                                json={"model": model["name"], "keep_alive": 0},
                            )

        await asyncio.sleep(1)

        # Stop and restart Ollama
        is_windows = platform.system() == "Windows"
        if is_windows:
            stop_cmd = "Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force"
            start_cmd = "Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden"
            subprocess.run(
                ["powershell", "-c", stop_cmd],
                capture_output=True,
                timeout=10,
            )
            await asyncio.sleep(3)
            subprocess.Popen(
                ["powershell", "-c", start_cmd],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        else:
            subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True, timeout=10)
            await asyncio.sleep(3)
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Wait for Ollama to come back
        await asyncio.sleep(4)
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(10):
                try:
                    r = await client.get("http://localhost:11434/api/tags")
                    if r.status_code == 200:
                        models = r.json().get("models", [])
                        logger.info("Ollama restarted — %d models available", len(models))
                        return {
                            "status": "restarted",
                            "models_available": len(models),
                            "message": "Ollama restarted successfully",
                        }
                except Exception:
                    pass
                await asyncio.sleep(1)

        return {
            "status": "restarted",
            "models_available": 0,
            "message": "Ollama restart command sent but not yet responding — check manually",
        }
    except Exception as e:
        logger.exception("Failed to restart Ollama: %s", e)
        raise HTTPException(500, f"Restart failed: {e}") from e


@app.put("/api/settings/log-level")
async def set_log_level(
    body: dict[str, str],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Change the server log level at runtime (admin only)."""
    if _auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to change server settings")
    level = body.get("level", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise HTTPException(422, f"Invalid log level: {level}")
    logging.getLogger("delegatron").setLevel(getattr(logging, level))
    logger.info("Log level changed to %s", level)
    return {"status": "ok", "level": level}


@app.put("/api/settings/log-timezone")
async def set_log_timezone(
    body: dict[str, str],
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Switch log timestamps between UTC and local timezone at runtime (admin only)."""
    if _auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to change server settings")
    import delegatron.utils.logging as log_mod

    tz = body.get("timezone", "utc").lower()
    if tz not in ("utc", "local"):
        raise HTTPException(422, f"Invalid timezone: {tz}. Use 'utc' or 'local'.")
    log_mod.LOG_TIMEZONE = tz
    logger.info("Log timezone changed to %s", tz)
    return {"status": "ok", "timezone": tz}


def _check_ollama_sync() -> dict[str, Any]:
    """Probe the local Ollama instance (blocking — call from to_thread)."""
    import urllib.request

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            return {"running": True, "models": models, "url": base}
    except Exception:
        return {"running": False, "models": [], "url": base}


async def _check_ollama() -> dict[str, Any]:
    """Probe the local Ollama instance without blocking the event loop."""
    return await asyncio.to_thread(_check_ollama_sync)


def _collect_ollama_stats() -> dict[str, Any]:
    """Collect Ollama GPU stats (blocking — call from to_thread)."""
    try:
        import urllib.request

        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        req = urllib.request.Request(f"{base}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = []
            for m in data.get("models", []):
                models.append(
                    {
                        "name": m.get("name", ""),
                        "size_vram_gb": round(
                            m.get("size_vram", 0) / 1_073_741_824,
                            1,
                        ),
                        "context_length": m.get("context_length", 0),
                    }
                )
            return {
                "models_loaded": len(models),
                "models": models,
                "total_vram_gb": round(
                    sum(m["size_vram_gb"] for m in models),
                    1,
                ),
            }
    except Exception:
        return {"models_loaded": 0, "models": [], "total_vram_gb": 0}


def _collect_docker_stats() -> dict[str, Any]:
    """Collect Docker container stats (blocking — call from to_thread)."""
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--format",
                "{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                containers.append(
                    {
                        "id": parts[0][:12],
                        "image": parts[1],
                        "status": parts[2],
                        "name": parts[3] if len(parts) > 3 else "",
                    }
                )
        # Get CPU/mem usage for running containers
        if containers:
            try:
                stat_result = subprocess.run(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                for line in stat_result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        cid = parts[0][:12]
                        for c in containers:
                            if c["id"] == cid:
                                c["cpu"] = parts[1]
                                c["mem"] = parts[2]
            except Exception:
                pass

        return {
            "containers_running": len(containers),
            "containers": containers,
            "mode": subprocess.run(
                ["docker", "version", "--format", "{{.Server.Os}}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout.strip()
            or "unknown",
        }
    except Exception:
        return {
            "containers_running": 0,
            "containers": [],
            "mode": "unknown",
        }


@app.get("/api/system/stats")
async def get_system_stats() -> dict[str, Any]:
    """Return live system stats: GPU, Ollama models, Docker containers.

    All blocking subprocess/network calls are run in a thread pool.
    """
    stats: dict[str, Any] = {}

    # Ollama loaded models (GPU usage) — run in thread
    stats["gpu"] = await asyncio.to_thread(_collect_ollama_stats)

    # Docker containers — run in thread
    stats["docker"] = await asyncio.to_thread(_collect_docker_stats)

    # Active pipeline runs (in-memory, non-blocking)
    running = [rid for rid, t in _run_tasks.items() if not t.done()]
    stats["pipeline"] = {
        "active_runs": len(running),
        "run_ids": running,
        "gpu_available": not _run_semaphore.locked(),
    }

    return stats


@app.get("/api/queue")
async def get_queue_status() -> dict[str, Any]:
    """Return current run queue status."""
    running = [rid for rid, task in _run_tasks.items() if not task.done()]
    return {
        "running": running,
        "running_count": len(running),
        "gpu_available": not _run_semaphore.locked(),
    }


@app.get("/api/defaults")
async def get_defaults() -> dict[str, Any]:
    """Return server defaults from .env for the dashboard to use as form defaults.

    This lets the GUI pre-populate from the server config so users see
    what's actually configured rather than hardcoded values.
    """
    ollama_status = await _check_ollama()
    return {
        "sandbox": os.environ.get("DELEGATRON_SANDBOX", "docker"),
        "max_iterations": int(os.environ.get("DELEGATRON_MAX_ITERATIONS", "5")),
        "max_cost_usd": float(os.environ.get("DELEGATRON_MAX_COST_USD", "5.0")),
        "max_wall_time_minutes": int(os.environ.get("DELEGATRON_MAX_WALL_TIME", "60")),
        "log_level": logging.getLevelName(logger.getEffectiveLevel()),
        "log_timezone": _get_log_timezone(),
        "workspace": str(_workspace_root),
        "auth_enabled": _auth_enabled(),
        "provider_keys": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
            "e2b": bool(os.environ.get("E2B_API_KEY")),
            "ollama": ollama_status["running"],
        },
        "ollama": ollama_status,
        "parallel_tasks": _is_feature_enabled("DELEGATRON_PARALLEL_TASKS"),
        "project_mode": _is_feature_enabled("DELEGATRON_PROJECT_MODE"),
    }


def _is_feature_enabled(env_var: str) -> bool:
    """Check if a feature flag env var is truthy."""
    return os.environ.get(env_var, "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Settings update endpoints for parallel/project mode
# ---------------------------------------------------------------------------

# In-memory overrides (persisted via env on restart, or saved to .env by user)
_feature_flags: dict[str, bool] = {
    "parallel_tasks": _is_feature_enabled("DELEGATRON_PARALLEL_TASKS"),
    "project_mode": _is_feature_enabled("DELEGATRON_PROJECT_MODE"),
}


@app.put("/api/settings/parallel-tasks")
async def set_parallel_tasks(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Enable or disable parallel task execution (admin only)."""
    if _auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to change server settings")
    body = await request.json()
    enabled = body.get("enabled", False)
    _feature_flags["parallel_tasks"] = bool(enabled)
    os.environ["DELEGATRON_PARALLEL_TASKS"] = "true" if enabled else "false"
    return {"parallel_tasks": _feature_flags["parallel_tasks"]}


@app.put("/api/settings/project-mode")
async def set_project_mode(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Enable or disable project mode (admin only)."""
    if _auth_enabled() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to change server settings")
    body = await request.json()
    enabled = body.get("enabled", False)
    _feature_flags["project_mode"] = bool(enabled)
    os.environ["DELEGATRON_PROJECT_MODE"] = "true" if enabled else "false"
    return {"project_mode": _feature_flags["project_mode"]}


# ---------------------------------------------------------------------------
# Project mode API endpoints
# ---------------------------------------------------------------------------

_active_projects: dict[str, dict[str, Any]] = {}


@app.post("/api/projects")
async def create_project(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Start a new project mode run (multi-milestone orchestration)."""
    if not _feature_flags.get("project_mode"):
        raise HTTPException(
            status_code=403,
            detail="Project mode is disabled. Enable via PUT /api/settings/project-mode",
        )

    body = await request.json()
    task = body.get("request", "")
    if not task:
        raise HTTPException(400, "Missing 'request' field")

    preset = body.get("preset", "default")
    max_milestones = body.get("max_milestones", 10)
    max_iterations = body.get("max_iterations", 5)
    max_cost = body.get("max_cost_usd", 50.0)
    max_time = body.get("max_wall_time_minutes", 120)

    project_id = f"proj-{uuid.uuid4().hex[:12]}"

    project_data: dict[str, Any] = {
        "project_id": project_id,
        "status": "running",
        "request": task,
        "preset": preset,
        "milestones_completed": 0,
        "milestones_total": 0,
        "results": [],
        "plan": None,
        "total_cost_usd": 0,
        "total_time_seconds": 0,
        "team_id": user.team_id if _auth_enabled() else None,
    }
    _active_projects[project_id] = project_data

    # Persist initial state
    proj_dir = _data_dir / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{project_id}.json").write_text(json.dumps(project_data, indent=2, default=str))

    # Launch background task
    task_obj = asyncio.create_task(
        _execute_project(
            project_id,
            task,
            preset,
            max_milestones,
            max_iterations,
            max_cost,
            max_time,
        )
    )
    _background_tasks.add(task_obj)
    task_obj.add_done_callback(_background_tasks.discard)

    return {
        "project_id": project_id,
        "status": "running",
        "message": f"Project started ({preset}). Track at /api/projects/{project_id}",
    }


@app.get("/api/projects/{project_id}")
async def get_project(
    project_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get project status and results."""
    # Check in-memory first (active project)
    if project_id in _active_projects:
        data = _active_projects[project_id]
        if _auth_enabled() and data.get("team_id") != user.team_id:
            raise HTTPException(status_code=403, detail="Access denied")
        return data

    proj_file = _data_dir / "projects" / f"{project_id}.json"
    if not proj_file.exists():
        raise HTTPException(404, f"Project '{project_id}' not found")

    data = _safe_load_json(proj_file) or {}
    if not data:
        raise HTTPException(500, f"Corrupt project data for '{project_id}'")

    if _auth_enabled() and data.get("team_id") != user.team_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return data


@app.get("/api/projects")
async def list_projects(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List project mode runs (filtered by team when auth is enabled)."""
    proj_dir = _data_dir / "projects"
    if not proj_dir.exists():
        return []

    projects: list[dict[str, Any]] = []
    for f in sorted(proj_dir.glob("proj-*.json"), reverse=True):
        data = _safe_load_json(f)
        if not data:
            continue
        if _auth_enabled() and data.get("team_id") != user.team_id:
            continue
        projects.append(
            {
                "project_id": data.get("project_id", f.stem),
                "status": data.get("status", "unknown"),
                "request": (data.get("request") or "")[:100],
                "preset": data.get("preset", ""),
                "milestones_completed": data.get("milestones_completed", 0),
                "milestones_total": data.get("milestones_total", 0),
                "total_cost_usd": data.get("total_cost_usd", 0),
            }
        )
    return projects


async def _execute_project(
    project_id: str,
    request_text: str,
    preset: str,
    max_milestones: int,
    max_iterations: int,
    max_cost: float,
    max_time: int,
) -> None:
    """Background task: run project mode."""
    from delegatron.project import ProjectRunner

    proj_dir = _data_dir / "projects"
    project_data = _active_projects.get(project_id, {})

    try:
        config = load_config(preset=preset)

        # Isolate the project workspace to prevent collisions with other runs
        project_workspace = _workspace_root / ".delegatron-runs" / project_id
        if not project_workspace.exists():
            await asyncio.to_thread(
                _init_run_workspace,
                _workspace_root,
                project_workspace,
                project_id,
            )

        runner = ProjectRunner(
            config=config,
            workspace_root=project_workspace,
            project_id=project_id,
            artifacts_dir=_artifacts_base,
            max_milestones=max_milestones,
            max_iterations_per_milestone=max_iterations,
            max_total_cost=max_cost,
            max_wall_time_minutes=max_time,
        )

        report = await runner.execute(request_text)
        report_dict = report.to_dict()

        project_data.update(
            {
                "status": report_dict["status"],
                "plan": report_dict["plan"],
                "results": report_dict["results"],
                "milestones_completed": report_dict["milestones_completed"],
                "milestones_total": report_dict["milestones_total"],
                "total_cost_usd": report_dict["total_cost_usd"],
                "total_time_seconds": report_dict["total_time_seconds"],
                "completed_at": report_dict["completed_at"],
            }
        )

    except Exception as e:
        logger.exception("Project %s failed: %s", project_id, e)
        project_data.update({"status": "failed", "error": str(e)})

    # Persist final state
    (proj_dir / f"{project_id}.json").write_text(json.dumps(project_data, indent=2, default=str))

    await asyncio.sleep(2)
    _active_projects.pop(project_id, None)


@app.post("/api/runs", response_model=RunResponse)
async def create_run(
    req: RunRequest,
    user: AuthUser = Depends(get_current_user),
) -> RunResponse:
    """Start a new pipeline run in the background."""
    return await _start_run_internal(req, user)


class PlanResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Response from a plan-only request."""

    run_id: str
    status: str
    plan_summary: str = ""
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    plan: dict[str, Any] = Field(default_factory=dict)


class EstimateRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request body for cost estimation."""

    preset: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    architect: RoleOverride | None = None
    implementer: RoleOverride | None = None
    reviewer: RoleOverride | None = None
    max_iterations: int = Field(default=5, ge=1, le=100)


class EstimateResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Estimated cost range for a pipeline run."""

    min_cost_usd: float
    max_cost_usd: float
    breakdown: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)


@app.post("/api/runs/plan", response_model=PlanResponse)
async def plan_only(
    req: RunRequest,
    user: AuthUser = Depends(get_current_user),
) -> PlanResponse:
    """Run only the architect step and return the plan synchronously.

    This is the API equivalent of ``delegatron plan``.  No code is generated,
    no sandbox is launched, and no background task is created.
    """
    from delegatron.config import PRESETS
    from delegatron.orchestrator.graph import Pipeline
    from delegatron.utils.cost import CostTracker

    # Validate preset
    if req.preset and req.preset not in PRESETS:
        valid = ", ".join(sorted(PRESETS.keys()))
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset {req.preset!r}. Valid presets: {valid}",
        )

    config = _apply_role_overrides(load_config(config_path=None, preset=req.preset), req)

    run_id = f"plan-{uuid.uuid4().hex[:12]}"
    run_workspace = await _resolve_request_workspace_or_http(req.workspace, req.branch)

    pipeline = Pipeline(
        config=config,
        workspace_root=run_workspace,
        run_id=run_id,
        artifacts_dir=_artifacts_base,
    )

    # Build minimal initial state and run only the architect step
    from delegatron.detection import detect_project

    project_info = detect_project(run_workspace)
    workspace_obj = Workspace(repo_path=run_workspace, run_id=run_id)

    state: dict[str, Any] = {
        "messages": [],
        "request": req.request,
        "project_info": {
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "test_commands": project_info.test_commands,
            "lint_commands": project_info.lint_commands,
            "package_manager": project_info.package_manager,
        },
        "file_tree": workspace_obj.get_file_tree(),
        "workspace_root": str(run_workspace),
        "plan": None,
        "current_task": None,
        "current_patch": None,
        "test_report": None,
        "review_report": None,
        "previous_review": None,
        "iteration": 0,
        "is_approved": False,
        "total_cost_usd": 0.0,
        "status": "running",
        "error": "",
    }

    cost_tracker = CostTracker()
    architect_agent = pipeline._create_architect()

    try:
        state = await pipeline._run_architect(state, architect_agent, cost_tracker)  # type: ignore[assignment,arg-type]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Architect failed: {e}") from e

    if state["status"] == "error":
        raise HTTPException(status_code=422, detail=str(state.get("error", "Architect failed")))

    plan: dict[str, Any] = state.get("plan") or {}
    return PlanResponse(
        run_id=run_id,
        status="planned",
        plan_summary=str(plan.get("summary", "")),
        tasks=plan.get("tasks", []),
        assumptions=plan.get("assumptions", []),
        cost_usd=cost_tracker.total_cost_usd,
        plan=plan,
    )


class ReviewRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request body for read-only codebase or log file review."""

    workspace: str | None = None
    # Branch to clone when ``workspace`` is a git URL (default: the remote's default branch).
    branch: str | None = Field(default=None, max_length=255, pattern=BRANCH_PATTERN)
    focus: str | None = Field(default=None, pattern=r"^(security|bugs|performance|all)$")
    question: str | None = None  # Specific question to focus the review on
    with_suggestions: bool = False  # Also run architect to propose improvements
    preset: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    log_content: str | None = None  # Raw log text to analyze instead of source code
    # Optional per-role overrides (applied on top of preset)
    architect: RoleOverride | None = None
    reviewer: RoleOverride | None = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str | None) -> str | None:
        return _validate_workspace_value(v)


class ReviewResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Response from a codebase review."""

    review_id: str
    summary: str
    accepted: bool
    issues: list[dict[str, Any]]
    files_reviewed: int
    cost_usd: float
    project: dict[str, Any]
    suggestions: dict[str, Any] | None = None  # Architect improvement plan


async def _invoke_and_book(
    agent: Any, input_data: dict[str, Any], role_config: RoleConfig, role: str, cost_tracker: Any
) -> dict[str, Any]:
    """Invoke *agent* once and book the call (local GPU time, cache tokens) on *cost_tracker*."""
    from delegatron.config import is_local_model
    from delegatron.utils.cost import get_gpu_power_watts

    local = is_local_model(role_config)
    start = time.monotonic()
    result: dict[str, Any] = await agent.invoke(input_data)
    generation_seconds = time.monotonic() - start
    meta = result.pop("_meta", {}) or {}
    cost_tracker.record(
        meta.get("model") or role_config.model,
        meta.get("input_tokens", 0),
        meta.get("output_tokens", 0),
        request_id=meta.get("request_id", ""),
        provider_request_id=meta.get("provider_request_id", ""),
        role=role,
        generation_seconds=generation_seconds,
        gpu_watts=await asyncio.to_thread(get_gpu_power_watts) if local else None,
        is_local=local,
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        temperature=meta.get("temperature"),
    )
    return result


@app.post("/api/review")
async def review_codebase(
    req: ReviewRequest,
    user: AuthUser = Depends(get_current_user),
) -> ReviewResponse:
    """Review an existing codebase (read-only). No code changes made.

    Scans the repo and produces a structured report covering bugs,
    security issues, code quality, and improvement suggestions.
    Optionally runs the architect agent to propose an improvement plan.
    """
    from delegatron.context import (
        REVIEW_FOCUS_INSTRUCTIONS,
        gather_review_context,
        review_budget_bytes,
    )
    from delegatron.detection import detect_project
    from delegatron.factories.agent_factory import create_reviewer
    from delegatron.utils.cost import CostTracker

    config = _apply_role_overrides(load_config(config_path=None, preset=req.preset or "local"), req)
    review_id = f"review-{uuid.uuid4().hex[:8]}"

    workspace_path = await _resolve_request_workspace_or_http(req.workspace, req.branch)
    if not workspace_path.exists():
        raise HTTPException(404, f"Workspace not found: {workspace_path}")

    reviewer_agent = create_reviewer(config)

    project_info = detect_project(workspace_path)
    ws = Workspace(repo_path=workspace_path, run_id=review_id)
    cost_tracker = CostTracker()

    is_log_mode = bool(req.log_content)
    # Listed once: the reviewer reads from it and the architect plans against it.
    file_tree = ws.get_file_tree() if not is_log_mode or req.with_suggestions else []
    review_files: dict[str, str] = {}

    if is_log_mode:
        # Log analysis mode
        log_text = req.log_content or ""
        lines = log_text.split("\n")
        if len(lines) > 2000:
            log_text = "\n".join(lines[-2000:])
        source_dump = f"\n--- Log Content ({len(lines)} lines) ---\n{log_text}\n"
        files_reviewed = 1
    else:
        # The source files, most significant first, sized to the reviewer's
        # context window when it is known (as the CLI ``review`` does); they go
        # in as files the reviewer agent can trim to fit, after the repo map.
        review_context = gather_review_context(
            workspace_path,
            file_tree=file_tree,
            languages=project_info.languages,
            max_total_size=review_budget_bytes(config.reviewer.context_window),
        )
        repo_map = review_context.repo_map
        source_dump = f"\n--- Repository Structure ---\n{repo_map}\n" if repo_map else ""
        review_files = dict(review_context.files)
        files_reviewed = len(review_files)

    focus_instruction = ""
    if req.focus and req.focus != "all":
        focus_instruction = REVIEW_FOCUS_INSTRUCTIONS.get(req.focus, "")

    user_question = ""
    if req.question:
        user_question = f"\n\nUser's specific question: {req.question}"

    if is_log_mode:
        request_text = (
            f"Analyze these log files. Identify: errors, warnings, recurring patterns, "
            f"root causes, and actionable fixes. For each issue found, explain what happened, "
            f"why it happened, and how to fix it. {focus_instruction}{user_question}"
        )
    else:
        request_text = (
            f"Review this existing codebase for quality, bugs, and security issues. "
            f"This is a READ-ONLY review — do not suggest a rewrite, focus on actionable findings. "
            f"{focus_instruction}{user_question}\n\n"
            f"Project: {workspace_path.name}\n"
            f"Languages: {', '.join(project_info.languages)}\n"
            f"Frameworks: {', '.join(project_info.frameworks)}\n"
        )

    input_data: dict[str, Any] = {
        "request": request_text,
        "diff": source_dump,
    }
    if review_files:
        input_data["related_files"] = review_files

    result = await _invoke_and_book(
        reviewer_agent, input_data, config.reviewer, "reviewer", cost_tracker
    )

    # Optionally run architect for improvement suggestions
    suggestions = None
    if req.with_suggestions:
        from delegatron.factories.agent_factory import create_architect

        architect_agent = create_architect(config)

        issues_text = "\n".join(
            f"- [{i.get('severity', 'info')}] {i.get('title', '')}: {i.get('details', '')}"
            for i in (result.get("issues", []) if isinstance(result.get("issues"), list) else [])
        )

        arch_input: dict[str, Any] = {
            "request": (
                f"Based on a code review of {workspace_path.name}, create an improvement plan. "
                f"The review found these issues:\n{issues_text}\n\n"
                f"Create a prioritized action plan to address these issues. "
                f"Focus on the highest-severity items first."
            ),
            "project_info": {
                "languages": project_info.languages,
                "frameworks": project_info.frameworks,
            },
            "file_tree": file_tree,
        }

        suggestions = await _invoke_and_book(
            architect_agent, arch_input, config.architect, "architect", cost_tracker
        )

    # Save review artifact in workspace reviews/ directory (user's data)
    reviews_base = _reviews_dir()
    store = ArtifactStore(base_dir=reviews_base, run_id=review_id)
    store.run_dir.mkdir(parents=True, exist_ok=True)
    review_data = {
        "review_id": review_id,
        "result": result,
        "suggestions": suggestions,
        "cost": cost_tracker.get_summary(),
        "project": {
            "path": str(workspace_path),
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
            "files_reviewed": files_reviewed,
        },
    }
    (store.run_dir / "summary.json").write_text(json.dumps(review_data, indent=2, default=str))

    # Unload models from GPU after review
    try:
        from delegatron.utils.gpu_cleanup import cleanup_after_run

        cleanup_after_run(config)
    except Exception:
        logger.debug("GPU cleanup after review failed (non-fatal)")

    return ReviewResponse(
        review_id=review_id,
        summary=result.get("summary", ""),
        accepted=result.get("acceptance_met", False),
        issues=result.get("issues", []) if isinstance(result.get("issues"), list) else [],
        files_reviewed=files_reviewed,
        cost_usd=cost_tracker.total_cost_usd,
        project={
            "path": str(workspace_path),
            "languages": project_info.languages,
            "frameworks": project_info.frameworks,
        },
        suggestions=suggestions,
    )


@app.get("/api/reviews")
async def list_reviews(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all past code reviews with summary info."""
    reviews = []
    reviews_base = _reviews_dir()
    for run_dir in sorted(reviews_base.glob("review-*")):
        summary_path = run_dir / "summary.json"
        if summary_path.exists():
            data = _safe_load_json(summary_path)
            if data:
                # Filter by team when auth is enabled
                if _auth_enabled() and data.get("team_id") != user.team_id:
                    continue
                result = data.get("result", {})
                project = data.get("project", {})
                reviews.append(
                    {
                        "review_id": data.get("review_id", run_dir.name),
                        "summary": result.get("summary", ""),
                        "accepted": result.get("acceptance_met", False),
                        "issue_count": len(result.get("issues", [])),
                        "files_reviewed": project.get("files_reviewed", 0),
                        "path": project.get("path", ""),
                        "languages": project.get("languages", []),
                    }
                )
    return list(reversed(reviews))


@app.post("/api/estimate", response_model=EstimateResponse)
async def estimate_cost(
    req: EstimateRequest,
    user: AuthUser = Depends(get_current_user),
) -> EstimateResponse:
    """Estimate the cost range for a pipeline run based on model pricing.

    Roles come from the preset with the request's overrides applied, as for a
    run.  Each role is local or cloud by its endpoint (``is_local_model``),
    never by its model name: a local role has no API cost and contributes to
    the ``local_gpu`` estimate instead, a cloud role is priced per token.
    Returns min/max estimates assuming typical token usage per iteration.
    """
    from delegatron.config import PRESETS
    from delegatron.utils.cost import CostTracker, estimate_local_cost

    if req.preset and req.preset not in PRESETS:
        valid = ", ".join(sorted(PRESETS.keys()))
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset {req.preset!r}. Valid presets: {valid}",
        )

    config = _apply_role_overrides(load_config(preset=req.preset or "default"), req)
    roles: dict[str, RoleConfig] = {name: getattr(config, name) for name in _ROLES}
    models = {name: role.model for name, role in roles.items()}
    local_roles = {name for name, role in roles.items() if is_local_model(role)}

    # Typical token usage estimates per call (input/output)
    # Min: small tasks, Max: complex tasks
    usage_estimates: dict[str, dict[str, tuple[int, int]]] = {
        "architect": {"min": (2000, 1000), "max": (8000, 4000)},
        "implementer": {"min": (3000, 2000), "max": (12000, 8000)},
        "reviewer": {"min": (4000, 1000), "max": (15000, 4000)},
    }

    tracker = CostTracker()
    breakdown: dict[str, Any] = {}
    total_min = 0.0
    total_max = 0.0
    for name, role in roles.items():
        # The architect runs once; implementer and reviewer once per iteration.
        calls = 1 if name == "architect" else req.max_iterations
        local = name in local_roles
        min_in, min_out = usage_estimates[name]["min"]
        max_in, max_out = usage_estimates[name]["max"]
        cost_min = cost_max = 0.0  # a local role is billed by GPU time (local_gpu below)
        if not local:
            cost_min = tracker.estimate_cost(role.model, min_in, min_out, is_local=False) * calls
            cost_max = tracker.estimate_cost(role.model, max_in, max_out, is_local=False) * calls
        breakdown[name] = {
            "model": role.model,
            "calls": calls,
            "is_local": local,
            "min_cost_usd": round(cost_min, 6),
            "max_cost_usd": round(cost_max, 6),
        }
        total_min += cost_min
        total_max += cost_max

    # Local roles cost GPU time instead: estimate it from typical generation speed.
    if local_roles:
        # Typical local model throughput: ~30 tok/s output (varies by model/GPU)
        local_tok_per_sec = 30.0
        total_min_output_tokens = 0
        total_max_output_tokens = 0
        for name in local_roles:
            multiplier = 1 if name == "architect" else req.max_iterations
            total_min_output_tokens += usage_estimates[name]["min"][1] * multiplier
            total_max_output_tokens += usage_estimates[name]["max"][1] * multiplier

        min_gen_seconds = total_min_output_tokens / local_tok_per_sec
        max_gen_seconds = total_max_output_tokens / local_tok_per_sec

        min_local = estimate_local_cost(min_gen_seconds)
        max_local = estimate_local_cost(max_gen_seconds)

        breakdown["local_gpu"] = {
            "roles": sorted(local_roles),
            "min_local_usd": min_local["total_local_usd"],
            "max_local_usd": max_local["total_local_usd"],
            "min_electricity_usd": min_local["electricity_usd"],
            "max_electricity_usd": max_local["electricity_usd"],
            "min_hardware_usd": min_local["hardware_usd"],
            "max_hardware_usd": max_local["hardware_usd"],
            "min_gpu_seconds": round(min_gen_seconds, 1),
            "max_gpu_seconds": round(max_gen_seconds, 1),
            "gpu_watts": min_local["gpu_watts"],
            "electricity_rate_kwh": min_local["electricity_rate_kwh"],
        }

        # Replace zero API costs with local estimates for total
        if total_min == 0:
            total_min = min_local["total_local_usd"]
        if total_max == 0:
            total_max = max_local["total_local_usd"]

    return EstimateResponse(
        min_cost_usd=round(total_min, 4),
        max_cost_usd=round(total_max, 4),
        breakdown=breakdown,
        models=models,
    )


@app.get("/api/runs")
async def list_runs(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all runs with their summaries, filtered by the user's team."""
    team_id = user.team_id if _auth_enabled() else None
    return _get_cached_runs(team_id)


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Cancel a running pipeline."""
    _verify_run_access(run_id, user)
    task = _run_tasks.get(run_id)
    if task is None:
        raise HTTPException(404, f"Run {run_id} is not currently running")
    if task.done():
        raise HTTPException(409, f"Run {run_id} has already completed")
    task.cancel()
    logger.info("Cancelled run %s", run_id)
    return {"status": "cancelled", "run_id": run_id}


@app.delete("/api/runs/{run_id}")
async def delete_run(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Delete a run, its artifacts and its run workspace. Cancels the run if still running."""
    import shutil

    _validate_run_id(run_id)
    _verify_run_access(run_id, user)

    # Cancel the running task and wait for it to fully stop
    task = _run_tasks.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        # Verify the task actually stopped before deleting artifacts
        if not task.done():
            raise HTTPException(
                409,
                f"Run {run_id} is still shutting down. Retry in a few seconds.",
            )
        logger.info("Cancelled running task for deleted run %s", run_id)

    run_dir = _artifacts_base / run_id
    if not run_dir.exists():
        raise HTTPException(404, f"Run {run_id} not found")
    run_workspace = recorded_run_workspace(run_id, _load_run_meta(run_dir))
    shutil.rmtree(run_dir, ignore_errors=True)
    if run_workspace is not None and run_workspace.is_dir():
        await asyncio.to_thread(_remove_run_workspace, run_id, run_workspace)
    _run_tasks.pop(run_id, None)
    _active_streams.pop(run_id, None)
    _invalidate_runs_cache()
    logger.info("Deleted run %s", run_id)
    return {"status": "deleted", "run_id": run_id}


@app.get("/api/runs/{run_id}/download")
async def download_run(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    """Download the code a completed run produced as a zip file.

    Bundles every file the run created or modified (across all iterations
    and tasks) and a generated README into a zip archive.
    """
    import io
    import zipfile

    _verify_run_access(run_id, user)
    run_dir = _artifacts_base / run_id

    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise HTTPException(404, f"Run '{run_id}' not found or not complete")

    summary = _safe_load_json(summary_path)
    if summary is None:
        raise HTTPException(500, f"Corrupt summary for run '{run_id}'")

    files = await asyncio.to_thread(collect_run_files, run_dir, _load_run_meta(run_dir))
    if not files:
        raise HTTPException(404, f"No generated files found for run '{run_id}'")

    # Generate README
    request_text = summary.get("request", "")
    plan_summary = summary.get("plan_summary", "")
    status = summary.get("status", "unknown")
    iterations = summary.get("iterations", 0)
    cost = summary.get("total_cost_usd", 0)
    elapsed = summary.get("elapsed_seconds", 0)

    readme = (
        f"# Delegatron Run: {run_id[:8]}\n\n"
        f"**Status:** {status}\n"
        f"**Iterations:** {iterations}\n"
        f"**Cost:** ${cost:.4f}\n"
        f"**Duration:** {elapsed:.1f}s\n\n"
        f"## Task\n\n{request_text}\n\n"
        f"## Plan\n\n{plan_summary}\n\n"
        f"## Generated Files\n\n"
    )
    for path in sorted(files.keys()):
        readme += f"- `{path}`\n"
    readme += (
        f"\n---\n*Generated by [Delegatron](https://delegatron.com) "
        f"on {summary.get('timestamp', 'unknown')}*\n"
    )

    # Build zip — sanitize paths to prevent Zip Slip attacks
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Use a distinct name to avoid collision with project README
        zf.writestr("DELEGATRON_RUN.md", readme)
        for path, content in sorted(files.items()):
            # Normalize and reject paths that escape the archive root
            safe_path = Path(path).as_posix()
            if safe_path.startswith("/") or ".." in safe_path.split("/"):
                logger.warning("Skipping unsafe path in zip: %s", path)
                continue
            zf.writestr(safe_path, content)

    buf.seek(0)

    # Sanitize filename — strip anything that isn't alphanumeric, hyphen, or underscore
    short_id = run_id[:8]
    task_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", request_text[:40]).strip("_")
    filename = f"delegatron-{short_id}-{task_slug}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs/{run_id}/compliance")
async def get_compliance_report(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Export the zero-cloud audit report for a completed run.

    The report lists every network destination the orchestrator used for the
    run (LLM endpoints, sandbox, git push) and flags the non-local ones as
    violations.  It is an audit of the configured destinations, not traffic
    monitoring or enforcement.  Its SHA-256 fingerprint is an integrity
    checksum over the whole report (recompute it to spot accidental edits);
    it is not a signature, so it cannot prove the report was not rewritten.
    """
    _verify_run_access(run_id, user)
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)

    zc_path = store.run_dir / "zero_cloud_report.json"
    if not zc_path.exists():
        # Fall back to summary-level data
        summary_path = store.run_dir / "summary.json"
        if not summary_path.exists():
            raise HTTPException(404, f"Run '{run_id}' not found")
        summary = _safe_load_json(summary_path)
        zc_summary = (summary or {}).get("zero_cloud")
        if zc_summary:
            return {"run_id": run_id, **zc_summary, "detail": "full report not available"}
        raise HTTPException(404, f"No compliance report for run '{run_id}'")

    report = _safe_load_json(zc_path)
    if report is None:
        raise HTTPException(500, f"Corrupt compliance report for run '{run_id}'")
    return report


def _extract_review_issues(run_dir: Path) -> str:
    """Return formatted issue text from the latest review report, or ''."""
    review_files = sorted(run_dir.glob("review_report_*.json"), reverse=True)
    if not review_files:
        return ""
    review = _safe_load_json(review_files[0])
    if not review:
        return ""
    issues = review.get("issues", [])
    if not issues:
        return ""
    issue_lines = []
    for issue in issues:
        severity = issue.get("severity", "major")
        desc = issue.get("description", issue.get("title", ""))
        if desc:
            issue_lines.append(f"- [{severity}] {desc}")
    if not issue_lines:
        return ""
    return "\n\nFix the following issues from the previous review:\n" + "\n".join(issue_lines)


def _load_summary_or_404(run_dir: Path) -> dict[str, Any]:
    """A run's summary.json: 404 when the run has none, 500 when it is unreadable."""
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise HTTPException(404, f"Run {run_dir.name} not found")
    summary = _safe_load_json(summary_path)
    if summary is None:
        raise HTTPException(500, f"Corrupt summary for run '{run_dir.name}'")
    return summary


def _original_request(run_dir: Path, summary: Mapping[str, Any]) -> str:
    """The request a run was started with: summary, else meta.json, else its checkpoint.

    The summaries written for cancelled runs and for runs that crashed outside
    the pipeline carry no request text.
    """
    text = summary.get("request") or _load_run_meta(run_dir).get("request")
    if not text and any(run_dir.glob("state_*.json")):
        try:
            state = ArtifactStore(base_dir=run_dir.parent, run_id=run_dir.name).load_state()
        except (OSError, ValueError):
            state = None
        text = (state or {}).get("request")
    return text if isinstance(text, str) else ""


@app.post("/api/runs/{run_id}/retry")
async def retry_run(
    run_id: str,
    body: RetryRequest | None = None,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Smart retry: auto-picks rerun vs refine based on review state.

    If the run has unresolved review issues and fresh_start is False,
    appends those issues to the request (refine behavior).
    Otherwise, does a clean rerun with the original request.
    """
    _verify_run_access(run_id, user)
    run_dir = _artifacts_base / run_id
    summary = _load_summary_or_404(run_dir)
    request_text = _original_request(run_dir, summary)
    if not request_text:
        raise HTTPException(400, "Original run has no request text")

    fresh = body.fresh_start if body else False
    strategy = "rerun"

    if not fresh:
        issues_text = _extract_review_issues(run_dir)
        if issues_text:
            request_text += issues_text
            strategy = "refine"

    meta = _load_run_meta(run_dir)

    # Apply overrides from the retry request body
    if body:
        if body.preset is not None:
            meta["preset"] = body.preset
        for role in ("architect", "implementer", "reviewer"):
            override = getattr(body, role, None)
            if override is not None:
                meta[role] = override.model_dump(exclude_defaults=True)

    req = _build_run_request_from_meta(request_text, meta)
    result = await _start_run_internal(req, user)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "message": result.message,
        "strategy": strategy,
    }


@app.post("/api/runs/{run_id}/rerun")
async def rerun(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> RunResponse:
    """Re-run with the same request. Prefer /retry."""
    result = await retry_run(
        run_id,
        RetryRequest(fresh_start=True),
        user,
    )
    return RunResponse(
        run_id=result["run_id"],
        status=result["status"],
        message=result["message"],
    )


# Final statuses a run can be resumed from: it stopped before every task was done.
_RESUMABLE_STATUSES = ("error", "cancelled", "stalled", "exhausted")


def _resumable_workspace(run_id: str, meta: Mapping[str, Any]) -> Path | None:
    """The run's own git workspace, if it still exists.

    Runs created before the workspace path was recorded in meta.json used
    ``<workspace>/.delegatron-runs/<run_id>`` under a local workspace.
    """
    workspace = recorded_run_workspace(run_id, meta)
    if workspace is None and "run_workspace" not in meta:
        base = meta.get("workspace")
        if not base:
            workspace = _workspace_root / RUNS_DIR_NAME / run_id
        elif not is_git_url(str(base)):
            workspace = Path(str(base)) / RUNS_DIR_NAME / run_id
    if workspace is None or not (workspace / ".git").exists():
        return None
    return workspace


def _resume_settings(meta: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """*meta* with the limits of earlier resumes and then *overrides* applied."""
    settings = dict(meta)
    for entry in meta.get("resumes") or []:
        if isinstance(entry, dict) and isinstance(entry.get("overrides"), dict):
            settings.update(entry["overrides"])
    settings.update(overrides)
    return settings


@app.post("/api/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: ResumeRequest | None = None,
    user: AuthUser = Depends(get_current_user),
) -> RunResponse:
    """Continue a stopped run from its latest checkpoint, as ``delegatron resume`` does.

    The run keeps its run_id, workspace and branch: the pipeline restarts from
    the last saved state (plan, finished tasks, iteration, reports) in the
    run's own workspace, or from the iteration the run was restored to
    (``POST /restore``).  Runs that ended ``error``, ``cancelled``,
    ``stalled`` or ``exhausted`` can be resumed; the optional body raises
    their limits (``max_iterations``, ``max_wall_time_minutes``,
    ``max_cost_usd``) for the continued run.  The resume is appended to
    meta.json ``resumes``; the original settings are kept.

    409 when the run is still active, has no checkpoint, or its workspace no
    longer exists (use /retry to start it again); 400 for other statuses.
    """
    _verify_run_access(run_id, user)
    if run_id in _run_tasks:
        raise HTTPException(409, f"Run {run_id} is still running")
    run_dir = _artifacts_base / run_id
    summary = _load_summary_or_404(run_dir)

    status = summary.get("status")
    if status not in _RESUMABLE_STATUSES:
        raise HTTPException(
            400,
            f"Run {run_id} has status '{status}' — only runs that ended "
            f"{', '.join(repr(s) for s in _RESUMABLE_STATUSES)} can be resumed",
        )

    # A run restored to an earlier iteration continues from that iteration,
    # whose files its workspace holds now.
    restored = summary.get("restored_to_iteration")
    iteration = restored if isinstance(restored, int) and not isinstance(restored, bool) else None
    try:
        saved_state = ArtifactStore(base_dir=_artifacts_base, run_id=run_id).load_state(
            iteration=iteration
        )
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"Corrupt checkpoint for run '{run_id}'") from e
    if saved_state is None:
        raise HTTPException(
            409,
            f"Run {run_id} has no checkpoint to resume from — use /retry to start it again",
        )

    meta = _load_run_meta(run_dir)
    workspace = _resumable_workspace(run_id, meta)
    if workspace is None:
        raise HTTPException(
            409,
            f"The workspace of run {run_id} no longer exists — use /retry to start it again",
        )

    request_text = saved_state.get("request") or _original_request(run_dir, summary)
    if not request_text:
        raise HTTPException(400, "Original run has no request text")

    overrides = body.model_dump(exclude_none=True) if body else {}
    req = _build_run_request_from_meta(request_text, _resume_settings(meta, overrides))
    if "run_workspace" not in meta:
        # A run from before workspaces were recorded: _execute_run finds it here.
        _update_run_meta(run_id, run_workspace=str(workspace.resolve()))
    return await _start_run_internal(
        req,
        user,
        initial_state=saved_state,
        resume=_Resume(run_id=run_id, from_status=str(status), overrides=overrides),
    )


@app.post("/api/runs/{run_id}/refine")
async def refine_run(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> RunResponse:
    """Re-run focusing on review issues. Prefer /retry."""
    result = await retry_run(
        run_id,
        RetryRequest(fresh_start=False),
        user,
    )
    return RunResponse(
        run_id=result["run_id"],
        status=result["status"],
        message=result["message"],
    )


@app.get("/api/runs/{run_id}")
async def get_run(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get the summary for a specific run."""
    _verify_run_access(run_id, user)
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
    summary_path = store.run_dir / "summary.json"

    if not summary_path.exists():
        # Check if run is active (task running or SSE stream open)
        if run_id in _run_tasks or run_id in _active_streams:
            entry: dict[str, Any] = {"run_id": run_id, "status": "running"}
            meta_path = store.run_dir / "meta.json"
            if meta_path.exists():
                meta = _safe_load_json(meta_path)
                if meta and meta.get("request"):
                    entry["request"] = meta["request"]
            return entry
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    summary = _safe_load_json(summary_path)
    if summary is None:
        raise HTTPException(status_code=500, detail=f"Corrupt summary for run '{run_id}'")
    summary = _live_status(store.run_dir, _repair_restored_summary(store.run_dir, summary))
    meta = _load_run_meta(store.run_dir)

    # Enrich with artifact details
    import glob

    files_created = await asyncio.to_thread(run_file_paths, store.run_dir, meta)
    if files_created:
        summary["files_created"] = files_created

    # Test results (from latest test report)
    reports = sorted(glob.glob(str(store.run_dir / "test_report_*.json")))
    if reports:
        latest_test = _safe_load_json(Path(reports[-1]))
        if latest_test:
            summary["test_report"] = {
                "passed": latest_test.get("passed", False),
                "total": latest_test.get("total", 0),
                "failures": latest_test.get("failures", 0),
                "stderr": (latest_test.get("stderr") or "")[-500:],
            }

    # Review details (from latest review)
    reviews = sorted(glob.glob(str(store.run_dir / "review_report_*.json")))
    if reviews:
        latest_review = _safe_load_json(Path(reviews[-1]))
        if latest_review:
            summary["review_details"] = {
                "summary": latest_review.get("summary", ""),
                "issues": latest_review.get("issues", []),
            }

    # Run config from meta.json (for retry dialog)
    if meta:
        summary["_meta"] = {
            "preset": meta.get("preset"),
            "sandbox": meta.get("sandbox"),
            "docker_image": meta.get("docker_image"),
            "max_iterations": meta.get("max_iterations"),
            "max_cost_usd": meta.get("max_cost_usd"),
            "architect": meta.get("architect"),
            "implementer": meta.get("implementer"),
            "reviewer": meta.get("reviewer"),
        }

    return summary


@app.get("/api/runs/{run_id}/files")
async def get_run_files(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get the contents of every file the run created or modified, across all iterations."""
    _verify_run_access(run_id, user)
    run_dir = _artifacts_base / run_id
    if not run_dir.is_dir():
        raise HTTPException(404, f"Run '{run_id}' not found")
    files = await asyncio.to_thread(collect_run_files, run_dir, _load_run_meta(run_dir))
    return {"files": files}


# ---------------------------------------------------------------------------
# Iteration history endpoints
# ---------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/iterations")
async def list_iterations(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """List all iteration checkpoints for a run with per-iteration metadata.

    Returns an array of iteration summaries including status, test results,
    review verdict, cost, and patch info for each iteration.
    """
    import glob as glob_mod

    _verify_run_access(run_id, user)
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)

    if not store.run_dir.exists():
        raise HTTPException(404, f"Run '{run_id}' not found")

    iterations: list[dict[str, Any]] = []

    state_files = sorted(glob_mod.glob(str(store.run_dir / "state_*.json")))
    for state_path in state_files:
        state_data = _safe_load_json(Path(state_path))
        if not state_data:
            continue
        iteration_num = state_data.get("iteration", 0)

        # Load corresponding artifacts for this iteration
        patch_path = store.run_dir / f"patch_set_{iteration_num:03d}.json"
        test_path = store.run_dir / f"test_report_{iteration_num:03d}.json"
        review_path = store.run_dir / f"review_report_{iteration_num:03d}.json"

        entry: dict[str, Any] = {
            "iteration": iteration_num,
            "status": state_data.get("status", "unknown"),
            "is_approved": state_data.get("is_approved", False),
            "patch_applied": state_data.get("patch_applied", False),
            "total_cost_usd": state_data.get("total_cost_usd", 0),
        }

        # Patch summary
        if patch_path.exists():
            patch_data = _safe_load_json(patch_path)
            if patch_data:
                files = list(patch_data.get("files", {}).keys())
                if not files:
                    diff = patch_data.get("diff_unified", "")
                    files = list(materialize_diff(diff).keys()) if diff else []
                entry["files_changed"] = files
                entry["rationale"] = (patch_data.get("rationale") or "")[:200]

        # Test summary
        if test_path.exists():
            test_data = _safe_load_json(test_path)
            if test_data:
                entry["tests"] = {
                    "passed": test_data.get("passed", False),
                    "total": test_data.get("total", 0),
                    "failures": test_data.get("failures", 0),
                }

        # Review summary
        if review_path.exists():
            review_data = _safe_load_json(review_path)
            if review_data:
                issues = review_data.get("issues", [])
                severity_counts: dict[str, int] = {}
                for issue in issues:
                    sev = issue.get("severity", "unknown")
                    severity_counts[sev] = severity_counts.get(sev, 0) + 1
                entry["review"] = {
                    "reviewer_accepted": review_data.get("acceptance_met", False),
                    "summary": (review_data.get("summary") or "")[:200],
                    "issue_counts": severity_counts,
                }

        iterations.append(entry)

    return {"run_id": run_id, "iterations": iterations}


@app.get("/api/runs/{run_id}/iterations/{iteration}/diff")
async def get_iteration_diff(
    run_id: str,
    iteration: int,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get the diff and full artifact details for a specific iteration."""
    _verify_run_access(run_id, user)
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)

    if not store.run_dir.exists():
        raise HTTPException(404, f"Run '{run_id}' not found")

    patch_path = store.run_dir / f"patch_set_{iteration:03d}.json"
    if not patch_path.exists():
        raise HTTPException(404, f"No patch for iteration {iteration}")

    patch_data = _safe_load_json(patch_path)
    if not patch_data:
        raise HTTPException(500, f"Corrupt patch for iteration {iteration}")

    result: dict[str, Any] = {
        "run_id": run_id,
        "iteration": iteration,
        "diff_unified": patch_data.get("diff_unified", ""),
        "rationale": patch_data.get("rationale", ""),
        "files": patch_data.get("files", {}),
    }

    # If files dict is empty but diff exists, materialize from diff
    if not result["files"] and result["diff_unified"]:
        result["files"] = materialize_diff(result["diff_unified"])

    # Include test and review data if available
    test_path = store.run_dir / f"test_report_{iteration:03d}.json"
    if test_path.exists():
        test_data = _safe_load_json(test_path)
        if test_data:
            result["test_report"] = test_data

    review_path = store.run_dir / f"review_report_{iteration:03d}.json"
    if review_path.exists():
        review_data = _safe_load_json(review_path)
        if review_data:
            result["review_report"] = review_data

    return result


class RestoreRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Body for /restore: the iteration whose workspace state to return to."""

    iteration: StrictInt = Field(ge=1)


@app.post("/api/runs/{run_id}/restore")
async def restore_iteration(
    run_id: str,
    body: RestoreRequest,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Reset a finished run's workspace to the commit of an earlier iteration.

    The run workspace's HEAD is reset (``git reset --hard``) to the last
    commit made at or before ``iteration``, so downloads and file views serve
    that state. The run keeps its final status; ``summary.json`` records
    ``restored_to_iteration``, ``restored_sha`` and ``pre_restore_sha`` (HEAD
    before the first restore, i.e. the run's final state). Returns 409 while
    the run is active or when it has no git workspace to restore.
    """
    _verify_run_access(run_id, user)
    run_dir = _artifacts_base / run_id
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise HTTPException(404, f"Run '{run_id}' not found")
    if run_id in _run_tasks:
        raise HTTPException(409, "Cannot restore while run is active — cancel first")
    summary = _safe_load_json(summary_path)
    if summary is None:
        raise HTTPException(500, f"Corrupt summary for run '{run_id}'")

    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
    try:
        state = store.load_state(iteration=body.iteration)
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"Corrupt checkpoint for iteration {body.iteration}") from e
    if not state:
        raise HTTPException(404, f"No checkpoint for iteration {body.iteration}")

    meta = _load_run_meta(run_dir)
    run_workspace = recorded_run_workspace(run_id, meta)
    sha = iteration_sha(state, body.iteration) or meta.get("workspace_init_sha") or ""
    if run_workspace is None or not run_workspace.is_dir() or not sha:
        raise HTTPException(409, "Run has no git workspace to restore")
    try:
        head = await asyncio.to_thread(_reset_run_workspace, run_workspace, run_id, sha)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(409, f"Could not reset the run workspace: {e}") from e

    summary = _repair_restored_summary(run_dir, summary)
    summary.setdefault("pre_restore_sha", head)
    summary["restored_to_iteration"] = body.iteration
    summary["restored_sha"] = sha
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    store.append_event({"event": "iteration_restored", "restored_to": body.iteration, "sha": sha})
    _invalidate_runs_cache()

    return {
        "status": "restored",
        "run_id": run_id,
        "restored_to_iteration": body.iteration,
        "restored_sha": sha,
    }


@app.get("/api/runs/{run_id}/plan")
async def get_pending_plan(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get the pending plan for interactive approval."""
    _verify_run_access(run_id, user)
    pending = _pending_approvals.get(run_id)
    if not pending:
        return {"status": "no_pending_plan"}
    return {"status": "pending", "plan": pending["plan"]}


@app.post("/api/runs/{run_id}/approve")
async def approve_plan(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Approve a pending plan."""
    _verify_run_access(run_id, user)
    pending = _pending_approvals.get(run_id)
    if not pending:
        raise HTTPException(404, "No pending plan for this run")
    pending["approved"] = True
    pending["event"].set()
    return {"status": "approved"}


@app.post("/api/runs/{run_id}/reject")
async def reject_plan(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Reject a pending plan."""
    _verify_run_access(run_id, user)
    pending = _pending_approvals.get(run_id)
    if not pending:
        raise HTTPException(404, "No pending plan for this run")
    pending["approved"] = False
    pending["event"].set()
    return {"status": "rejected"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of live pipeline events.

    Connects to an active run and streams events as they happen.
    If the run is already complete, streams the saved events from disk.
    """
    _verify_run_access(run_id, user)
    # Check for active stream
    queues = _active_streams.get(run_id)

    if queues is not None:
        # Create a new bounded queue for this subscriber so multiple clients
        # each receive all events independently (fan-out).
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        queues.append(queue)

        # Replay events already saved to disk, then stream live events.
        store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
        events_file = store.run_dir / "events.jsonl"
        return StreamingResponse(
            _sse_replay_then_live(events_file, queue, run_id, queues),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Fall back to saved events
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
    events_file = store.run_dir / "events.jsonl"

    if not events_file.exists():
        raise HTTPException(status_code=404, detail=f"No events for run '{run_id}'")

    return StreamingResponse(
        _sse_from_file(events_file),
        media_type="text/event-stream",
    )


@app.get("/api/analytics")
async def get_analytics(
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return basic analytics derived from cached run summaries."""
    team_id = user.team_id if _auth_enabled() else None
    runs = _get_cached_runs(team_id)

    # Count presets from cached data
    preset_counts: dict[str, int] = {}
    for r in runs:
        preset_name = r.get("_preset") or "default"
        preset_counts[preset_name] = preset_counts.get(preset_name, 0) + 1

    total_runs = len(runs)
    total_cost = sum(r.get("total_cost_usd", 0) for r in runs)
    total_iterations = sum(r.get("iterations", 0) for r in runs)
    approved = sum(1 for r in runs if r.get("is_approved"))

    runs_by_status: dict[str, int] = {}
    for r in runs:
        status = r.get("status", "unknown")
        runs_by_status[status] = runs_by_status.get(status, 0) + 1

    # Compute most-used preset from actual meta.json data
    most_used_preset = "default"
    if preset_counts:
        most_used_preset = max(preset_counts, key=lambda k: preset_counts[k])

    # Aggregate cost_by_model from summary cost_breakdown data
    cost_by_model: dict[str, float] = {}
    for r in runs:
        breakdown = r.get("cost_breakdown")
        if isinstance(breakdown, dict):
            for _role, role_data in breakdown.items():
                if isinstance(role_data, dict):
                    model_name = role_data.get("model", "")
                    model_cost = role_data.get("total_cost_usd", 0.0)
                    if model_name and isinstance(model_cost, (int, float)):
                        cost_by_model[model_name] = cost_by_model.get(model_name, 0.0) + model_cost
    # Round aggregated costs
    cost_by_model = {k: round(v, 4) for k, v in cost_by_model.items()}

    result: dict[str, Any] = {
        "total_runs": total_runs,
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_run": round(total_cost / total_runs, 4) if total_runs else 0,
        "avg_iterations": round(total_iterations / total_runs, 2) if total_runs else 0,
        "success_rate": round(approved / total_runs, 4) if total_runs else 0,
        "most_used_preset": most_used_preset,
        "runs_by_status": runs_by_status,
    }
    if cost_by_model:
        result["cost_by_model"] = cost_by_model
    return result


@app.get("/api/analytics/leaderboard")
async def model_leaderboard(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Model/preset performance leaderboard.

    Returns approval rate, avg cost, avg iterations for each preset used.
    """
    preset_stats: dict[str, dict[str, Any]] = {}

    if _artifacts_base.exists():
        for d in sorted(_artifacts_base.iterdir()):
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            summary_path = d / "summary.json"
            if not summary_path.exists():
                continue

            meta = _safe_load_json(meta_path) if meta_path.exists() else None
            summary = _safe_load_json(summary_path)
            if not summary:
                continue

            # Filter by team when auth is enabled
            if _auth_enabled() and (meta or {}).get("team_id") != user.team_id:
                continue

            preset = (meta or {}).get("preset", "default")
            if preset not in preset_stats:
                preset_stats[preset] = {
                    "preset": preset,
                    "runs": 0,
                    "approved": 0,
                    "total_cost": 0.0,
                    "total_iterations": 0,
                    "errors": 0,
                }

            stats = preset_stats[preset]
            stats["runs"] += 1
            if summary.get("is_approved"):
                stats["approved"] += 1
            if summary.get("status") == "error":
                stats["errors"] += 1
            stats["total_cost"] += summary.get("total_cost_usd", 0)
            stats["total_iterations"] += summary.get("iterations", 0)

    # Compute derived metrics
    result = []
    for stats in preset_stats.values():
        runs = stats["runs"]
        result.append(
            {
                "preset": stats["preset"],
                "runs": runs,
                "approval_rate": round(stats["approved"] / runs, 2) if runs else 0,
                "error_rate": round(stats["errors"] / runs, 2) if runs else 0,
                "avg_cost": round(stats["total_cost"] / runs, 4) if runs else 0,
                "total_cost": round(stats["total_cost"], 4),
                "avg_iterations": round(stats["total_iterations"] / runs, 1) if runs else 0,
            }
        )

    # Sort by approval rate descending, then by cost ascending
    result.sort(key=lambda x: (-x["approval_rate"], x["avg_cost"]))
    return result


# --- Template endpoints ---


@app.get("/api/templates")
async def list_templates(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all templates (built-in + user).  Each entry includes an
    ``is_builtin`` boolean so the dashboard can hide delete controls and
    badge built-ins differently."""
    out: list[dict[str, Any]] = []
    for t in _template_store.list_templates():
        data = t.to_dict()
        data["is_builtin"] = _template_store.is_builtin(t.name)
        out.append(data)
    return out


@app.post("/api/templates")
async def create_template(
    req: TemplateRequest,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Save a new template."""
    template = RunTemplate(
        name=req.name,
        description=req.description,
        request_pattern=req.request_pattern,
        preset=req.preset,
        overrides=req.overrides,
        constraints=req.constraints,
        variables=req.variables,
    )
    _template_store.save(template)
    return {"status": "saved", "name": req.name}


@app.delete("/api/templates/{name}")
async def delete_template(
    name: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    """Delete a template by name."""
    try:
        _template_store.delete(name)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Template '{name}' not found") from exc
    return {"status": "deleted", "name": name}


@app.post("/api/templates/{name}/run")
async def run_template(
    name: str,
    req: TemplateRunRequest | None = None,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Start a pipeline run from a saved template."""
    try:
        template = _template_store.load(name)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Template '{name}' not found") from exc

    variables = req.variables if req else {}
    try:
        request_text = template.render(variables)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Build RunRequest from template config
    run_kwargs: dict[str, Any] = {"request": request_text}
    # Use template preset, request preset, or fall back to local
    run_kwargs["preset"] = template.preset or (req and req.preset) or "local"
    if req and req.workspace:
        run_kwargs["workspace"] = req.workspace
    if req and req.sandbox:
        run_kwargs["sandbox"] = req.sandbox
    for key in ("max_iterations", "max_cost_usd", "max_wall_time_minutes"):
        if key in template.overrides:
            run_kwargs[key] = template.overrides[key]
    if template.overrides.get("interactive"):
        run_kwargs["interactive"] = template.overrides["interactive"]
    # Extract docker_image from pipeline overrides
    pipeline_ovr = template.overrides.get("pipeline", {})
    if isinstance(pipeline_ovr, dict) and pipeline_ovr.get("docker_image"):
        run_kwargs["docker_image"] = pipeline_ovr["docker_image"]
    if template.constraints:
        # Carried on the request so they are enforced and replayed by retry/resume.
        run_kwargs["constraints"] = template.constraints

    try:
        run_req = RunRequest(**run_kwargs)
    except ValidationError as exc:
        raise HTTPException(422, detail=_validation_errors(exc)) from exc
    resp = await _start_run_internal(run_req, user)
    data: dict[str, Any] = resp.model_dump()
    return data


# --- Benchmark endpoints ---


class BenchmarkRequest(BaseModel):  # type: ignore[misc, unused-ignore]
    """Request to start a benchmark comparison run."""

    request: str = Field(..., min_length=1, max_length=100_000)
    presets: list[str] = Field(..., min_length=1)
    max_iterations: int | None = Field(default=None, ge=1, le=100)
    sandbox: str | None = Field(default=None, pattern=r"^(docker|e2b)$")
    docker_image: str | None = Field(default=None, max_length=200)

    @field_validator("presets")
    @classmethod
    def validate_presets(cls, v: list[str]) -> list[str]:
        from delegatron.config import PRESETS as _PRESETS

        for preset in v:
            if preset not in _PRESETS:
                valid = ", ".join(sorted(_PRESETS.keys()))
                raise ValueError(f"Unknown preset {preset!r}. Valid presets: {valid}")
        return v


class BenchmarkResultResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Result of a single preset run within a benchmark."""

    preset: str
    status: str
    iterations: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    approved: bool = False
    error: str = ""


class BenchmarkResponse(BaseModel):  # type: ignore[misc, unused-ignore]
    """Response for benchmark status/results."""

    benchmark_id: str
    status: str  # "running" or "complete"
    request: str = ""
    presets: list[str] = Field(default_factory=list)
    results: list[BenchmarkResultResponse] = Field(default_factory=list)
    cheapest_passing: str | None = None


# In-memory store for active benchmark runs
# Maps benchmark_id -> benchmark data dict
_active_benchmarks: dict[str, dict[str, Any]] = {}


@app.post("/api/benchmark")
async def start_benchmark(
    req: BenchmarkRequest,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Start a benchmark comparison run in the background.

    Runs the given task against each preset sequentially and stores results.
    Returns a benchmark_id for tracking via GET /api/benchmark/{id}.
    """
    benchmark_id = f"bench-{uuid.uuid4().hex[:12]}"

    # Initialize benchmark tracking
    benchmark_data: dict[str, Any] = {
        "benchmark_id": benchmark_id,
        "status": "running",
        "request": req.request,
        "presets": req.presets,
        "max_iterations": req.max_iterations,
        "sandbox": req.sandbox,
        "results": [],
        "cheapest_passing": None,
        "team_id": user.team_id,
    }
    _active_benchmarks[benchmark_id] = benchmark_data

    # Persist initial state to data directory
    bench_dir = _data_dir / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)
    (bench_dir / f"{benchmark_id}.json").write_text(json.dumps(benchmark_data, default=str))

    # Launch benchmark in background
    task = asyncio.create_task(_execute_benchmark(benchmark_id, req))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "benchmark_id": benchmark_id,
        "status": "running",
        "message": f"Benchmark started with {len(req.presets)} presets. "
        f"Track at /api/benchmark/{benchmark_id}",
    }


@app.get("/api/benchmark/{benchmark_id}")
async def get_benchmark(
    benchmark_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get benchmark status and results."""
    # Check in-memory first (active benchmark)
    if benchmark_id in _active_benchmarks:
        data = _active_benchmarks[benchmark_id]
        if _auth_enabled() and data.get("team_id") != user.team_id:
            raise HTTPException(status_code=403, detail="Access denied")
        return {
            "benchmark_id": data["benchmark_id"],
            "status": data["status"],
            "request": data["request"],
            "presets": data["presets"],
            "results": data["results"],
            "cheapest_passing": data.get("cheapest_passing"),
        }

    # Fall back to persisted data
    bench_file = _data_dir / "benchmarks" / f"{benchmark_id}.json"
    if not bench_file.exists():
        raise HTTPException(status_code=404, detail=f"Benchmark '{benchmark_id}' not found")

    persisted = _safe_load_json(bench_file) or {}
    if not persisted:
        raise HTTPException(status_code=500, detail=f"Corrupt benchmark data for '{benchmark_id}'")

    if _auth_enabled() and persisted.get("team_id") != user.team_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "benchmark_id": persisted["benchmark_id"],
        "status": persisted["status"],
        "request": persisted["request"],
        "presets": persisted["presets"],
        "results": persisted["results"],
        "cheapest_passing": persisted.get("cheapest_passing"),
    }


# ---------------------------------------------------------------------------
# SWE-bench evaluation endpoints
# ---------------------------------------------------------------------------

_active_swebench: dict[str, dict[str, Any]] = {}


@app.post("/api/swebench")
async def start_swebench(
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Start a SWE-bench evaluation run.

    Runs Delegatron on real GitHub issues from the SWE-bench dataset
    and evaluates whether generated patches resolve the issues.
    """
    body = await request.json()
    preset = body.get("preset", "default")
    split = body.get("split", "lite")
    instances = body.get("instances", 10)
    tier = body.get("tier")
    max_iterations = body.get("max_iterations", 5)

    eval_id = f"swe-{uuid.uuid4().hex[:12]}"

    eval_data: dict[str, Any] = {
        "eval_id": eval_id,
        "status": "running",
        "preset": preset,
        "split": split,
        "total_instances": instances,
        "tier": tier,
        "results": [],
        "resolve_rate": 0,
        "total_cost_usd": 0,
        "by_tier": {},
        "team_id": user.team_id if _auth_enabled() else None,
    }
    _active_swebench[eval_id] = eval_data

    # Persist initial state
    bench_dir = _data_dir / "swebench"
    bench_dir.mkdir(parents=True, exist_ok=True)
    (bench_dir / f"{eval_id}.json").write_text(json.dumps(eval_data, indent=2, default=str))

    # Launch background task
    task = asyncio.create_task(
        _execute_swebench(
            eval_id,
            preset,
            split,
            instances,
            tier,
            max_iterations,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "eval_id": eval_id,
        "status": "running",
        "message": f"SWE-bench evaluation started ({instances} instances, {preset}). "
        f"Track at /api/swebench/{eval_id}",
    }


@app.get("/api/swebench/{eval_id}")
async def get_swebench(
    eval_id: str,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Get SWE-bench evaluation status and results."""
    # Check in-memory first
    if eval_id in _active_swebench:
        data = _active_swebench[eval_id]
        if _auth_enabled() and data.get("team_id") != user.team_id:
            raise HTTPException(status_code=403, detail="Access denied")
        return data

    # Fall back to persisted file
    bench_file = _data_dir / "swebench" / f"{eval_id}.json"
    if not bench_file.exists():
        raise HTTPException(404, f"SWE-bench evaluation '{eval_id}' not found")

    data = _safe_load_json(bench_file) or {}
    if not data:
        raise HTTPException(500, f"Corrupt SWE-bench data for '{eval_id}'")

    if _auth_enabled() and data.get("team_id") != user.team_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return data


@app.get("/api/swebench")
async def list_swebench(
    user: AuthUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List SWE-bench evaluations (filtered by team when auth is enabled)."""
    bench_dir = _data_dir / "swebench"
    if not bench_dir.exists():
        return []

    evals: list[dict[str, Any]] = []
    for f in sorted(bench_dir.glob("swe-*.json"), reverse=True):
        data = _safe_load_json(f)
        if not data:
            continue
        if _auth_enabled() and data.get("team_id") != user.team_id:
            continue
        evals.append(
            {
                "eval_id": data.get("eval_id", f.stem),
                "status": data.get("status", "unknown"),
                "preset": data.get("preset", ""),
                "split": data.get("split", ""),
                "total_instances": data.get("total_instances", 0),
                "completed": len(data.get("results", [])),
                "resolve_rate": data.get("resolve_rate", 0),
                "total_cost_usd": data.get("total_cost_usd", 0),
            }
        )
    return evals


async def _execute_swebench(
    eval_id: str,
    preset: str,
    split: str,
    instances: int,
    tier: str | None,
    max_iterations: int,
) -> None:
    """Background task: run SWE-bench evaluation."""
    from delegatron.swebench import SWEBenchAdapter

    bench_dir = _data_dir / "swebench"
    eval_data = _active_swebench.get(eval_id, {})

    try:
        # Isolate SWE-bench work in a per-eval workspace
        swe_workspace = _workspace_root / ".delegatron-runs" / eval_id
        if not swe_workspace.exists():
            await asyncio.to_thread(
                _init_run_workspace,
                _workspace_root,
                swe_workspace,
                eval_id,
            )

        adapter = SWEBenchAdapter(
            preset=preset,
            work_dir=swe_workspace,
            artifacts_dir=_artifacts_base,
            max_iterations=max_iterations,
        )

        report = await adapter.run(split=split, limit=instances, tier=tier)

        # Update with final results
        report_dict = report.to_dict()
        eval_data.update(
            {
                "status": "complete",
                "results": report_dict["results"],
                "resolve_rate": report_dict["resolve_rate"],
                "total_cost_usd": report_dict["total_cost_usd"],
                "total_time_seconds": report_dict["total_time_seconds"],
                "avg_iterations": report_dict["avg_iterations"],
                "by_tier": report_dict["by_tier"],
                "completed_at": report_dict["completed_at"],
            }
        )

    except Exception as e:
        logger.exception("SWE-bench evaluation %s failed: %s", eval_id, e)
        eval_data.update({"status": "error", "error": str(e)})

    # Persist final state
    (bench_dir / f"{eval_id}.json").write_text(json.dumps(eval_data, indent=2, default=str))

    # Clean up in-memory after a delay
    await asyncio.sleep(2)
    _active_swebench.pop(eval_id, None)


async def _execute_benchmark(
    benchmark_id: str,
    req: BenchmarkRequest,
) -> None:
    """Execute a benchmark run: run the task with each preset sequentially."""
    import time

    from delegatron.orchestrator.graph import Pipeline

    benchmark_data = _active_benchmarks[benchmark_id]
    bench_dir = _data_dir / "benchmarks"

    try:
        for preset_name in req.presets:
            start = time.monotonic()
            result: dict[str, Any] = {
                "preset": preset_name,
                "status": "running",
                "iterations": 0,
                "cost_usd": 0.0,
                "elapsed_seconds": 0.0,
                "approved": False,
                "error": "",
            }

            try:
                config = load_config(preset=preset_name)

                # Apply overrides
                from dataclasses import replace as _dc_replace

                pipeline_overrides: dict[str, Any] = {}
                if req.max_iterations is not None:
                    pipeline_overrides["max_iterations"] = req.max_iterations
                if req.sandbox is not None:
                    pipeline_overrides["sandbox"] = req.sandbox
                if req.docker_image is not None:
                    pipeline_overrides["docker_image"] = req.docker_image
                if pipeline_overrides:
                    config = _dc_replace(
                        config,
                        pipeline=_dc_replace(config.pipeline, **pipeline_overrides),
                    )

                run_id = f"{benchmark_id}-{preset_name}"
                # Isolate each benchmark preset run in its own workspace
                bench_workspace = _workspace_root / ".delegatron-runs" / run_id
                if not bench_workspace.exists():
                    await asyncio.to_thread(
                        _init_run_workspace,
                        _workspace_root,
                        bench_workspace,
                        run_id,
                    )

                pipeline = Pipeline(
                    config=config,
                    workspace_root=bench_workspace,
                    run_id=run_id,
                    artifacts_dir=_artifacts_base,
                )

                async with _run_semaphore:
                    state = await pipeline.execute(req.request)
                elapsed = time.monotonic() - start

                preset_cost = round(state.get("total_cost_usd", 0.0), 4)
                result.update(
                    {
                        "status": state.get("status", "unknown"),
                        "iterations": state.get("iteration", 0),
                        "cost_usd": preset_cost,
                        "elapsed_seconds": round(elapsed, 1),
                        "approved": state.get("is_approved", False),
                    }
                )

            except Exception as e:
                elapsed = time.monotonic() - start
                logger.exception("Benchmark preset '%s' failed for %s", preset_name, benchmark_id)
                result.update(
                    {
                        "status": "error",
                        "elapsed_seconds": round(elapsed, 1),
                        "error": str(e),
                    }
                )

            benchmark_data["results"].append(result)

            # Persist intermediate progress
            (bench_dir / f"{benchmark_id}.json").write_text(json.dumps(benchmark_data, default=str))

        # Find cheapest passing preset
        passing = [r for r in benchmark_data["results"] if r.get("approved")]
        if passing:
            cheapest = min(passing, key=lambda r: r["cost_usd"])
            benchmark_data["cheapest_passing"] = cheapest["preset"]

        benchmark_data["status"] = "complete"

    except Exception as e:
        logger.exception("Benchmark %s failed", benchmark_id)
        benchmark_data["status"] = "error"
        benchmark_data["error"] = str(e)

    finally:
        # Persist final state
        (bench_dir / f"{benchmark_id}.json").write_text(json.dumps(benchmark_data, default=str))
        # Remove from active benchmarks after a delay
        await asyncio.sleep(2)
        _active_benchmarks.pop(benchmark_id, None)


@app.post("/api/webhooks/github")
async def github_webhook(request: Request) -> dict[str, str]:
    """Handle GitHub webhook events.

    Supports:
    - issue_comment: trigger run from `/delegatron run <request>` comments
    - pull_request: (future) trigger review on PR events

    Requires DELEGATRON_WEBHOOK_SECRET to be set for signature verification.
    """
    # Reject webhook runs in multi-tenant mode — no repo→team binding yet
    if _auth_enabled():
        raise HTTPException(
            status_code=403,
            detail="Webhook-triggered runs are disabled when auth is enabled",
        )

    # Verify webhook signature
    body = await request.body()
    if not _verify_webhook_signature(request, body):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook payload") from exc
    event_type = request.headers.get("X-GitHub-Event", "")

    if event_type == "issue_comment":
        comment = payload.get("comment", {}).get("body", "")
        if comment.strip().startswith("/delegatron run"):
            task_text = comment.replace("/delegatron run", "").strip()
            if task_text and len(task_text) <= 10_000:
                run_id = uuid.uuid4().hex[:12]
                _active_streams[run_id] = []

                # Persist metadata so the run is discoverable and team-scoped
                webhook_team = "default"
                run_dir = _artifacts_base / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "meta.json").write_text(
                    json.dumps({"team_id": webhook_team, "user_id": "webhook"})
                )

                bg_task = asyncio.create_task(
                    _execute_run(
                        run_id,
                        RunRequest(request=task_text),
                    )
                )
                _background_tasks.add(bg_task)
                _run_tasks[run_id] = bg_task

                def _cleanup_webhook_task(
                    t: asyncio.Task[None],
                    rid: str = run_id,
                ) -> None:
                    _background_tasks.discard(t)
                    _run_tasks.pop(rid, None)
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.error("Webhook-triggered run %s failed: %s", rid, exc)

                bg_task.add_done_callback(_cleanup_webhook_task)
                return {
                    "status": "started",
                    "run_id": run_id,
                    "trigger": "issue_comment",
                }

    return {"status": "ignored", "event": event_type}


def _verify_webhook_signature(request: Request, body: bytes) -> bool:
    """Verify the GitHub webhook X-Hub-Signature-256 header."""
    if not _webhook_secret:
        logger.warning("DELEGATRON_WEBHOOK_SECRET not set — rejecting webhook")
        return False

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        _webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


# --- Authorization helpers ---


_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_run_id(run_id: str) -> None:
    """Reject run_id values that could escape the artifacts directory."""
    if not _RUN_ID_RE.match(run_id) or ".." in run_id:
        raise HTTPException(status_code=400, detail="Invalid run_id")


def _verify_run_access(run_id: str, user: AuthUser) -> None:
    """Verify the authenticated user has access to the given run.

    When auth is disabled, access is always granted.
    When auth is enabled, the run's meta.json must exist and its team_id
    must match the user's team_id.

    Raises:
        HTTPException(400) if the run_id contains path traversal characters.
        HTTPException(404) if the run directory or meta.json doesn't exist.
        HTTPException(403) if the team_id doesn't match.
    """
    _validate_run_id(run_id)
    if not _auth_enabled():
        return

    meta_path = _artifacts_base / run_id / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    meta = _safe_load_json(meta_path)
    if meta is None:
        raise HTTPException(status_code=500, detail=f"Corrupt metadata for run '{run_id}'")
    if meta.get("team_id") != user.team_id:
        raise HTTPException(status_code=403, detail="Access denied")


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file, returning None on corruption or read errors."""
    try:
        return cast(dict[str, Any], json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Corrupt or unreadable JSON at %s: %s", path, e)
        return None


def _last_event(events_file: Path, name: str) -> dict[str, Any] | None:
    """The last event called *name* in an events.jsonl file, if any."""
    if not events_file.is_file():
        return None
    last: dict[str, Any] | None = None
    for line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == name:
            last = event
    return last


def _repair_restored_summary(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """Undo the "running" status older restores copied from a checkpoint.

    Such a run is finished but would show as live forever; its real outcome
    is the run's last ``pipeline_complete`` event.  A run without one (it was
    cancelled, or failed outside the pipeline) is reported as ``error``, which
    can be resumed or retried.
    """
    if (
        summary.get("status") != "running"
        or "restored_to_iteration" not in summary
        or run_dir.name in _run_tasks
    ):
        return summary
    final = _last_event(run_dir / "events.jsonl", "pipeline_complete")
    if final is not None and final.get("status"):
        summary["status"] = final["status"]
        summary["is_approved"] = bool(final.get("is_approved", False))
    else:
        summary["status"] = "error"
        summary["is_approved"] = False
        summary["error"] = summary.get("error") or "The run ended before its pipeline finished"
    try:
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    except OSError as e:
        logger.warning("Could not save repaired summary for run %s: %s", run_dir.name, e)
    return summary


def _live_status(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """*summary* as the API reports it: a resumed run is "running" until its session ends.

    Until then summary.json still holds the previous session's outcome.
    """
    if run_dir.name in _resumed_live:
        return {**summary, "status": "running", "is_approved": False}
    return summary


def _reset_run_workspace(run_workspace: Path, run_id: str, sha: str) -> str:
    """Reset a run workspace to *sha* (blocking); return the HEAD it had before."""
    workspace = Workspace(repo_path=run_workspace, run_id=run_id)
    head = workspace.snapshot().commit_sha
    workspace.rollback(sha)
    return head


# --- Internal run-creation helpers ---


def _load_run_meta(run_dir: Path) -> dict[str, Any]:
    """Load meta.json from a run directory, returning empty dict on failure."""
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        return _safe_load_json(meta_path) or {}
    return {}


_ROLES = ("architect", "implementer", "reviewer")


@dataclass(frozen=True)
class _Resume:
    """A new session of an existing run (``POST /resume``)."""

    run_id: str
    from_status: str
    overrides: dict[str, Any]


def _update_run_meta(run_id: str, **fields: Any) -> None:
    """Merge *fields* into a run's meta.json."""
    run_dir = _artifacts_base / run_id
    meta = _load_run_meta(run_dir)
    meta.update(fields)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps(meta))


def _build_run_request_from_meta(request_text: str, meta: dict[str, Any]) -> RunRequest:
    """Reconstruct a RunRequest from saved meta.json fields (422 if no longer valid)."""
    kwargs: dict[str, Any] = {"request": request_text}
    for key in (
        "preset",
        "workspace",
        "branch",
        "sandbox",
        "docker_image",
        "max_iterations",
        "max_cost_usd",
        "max_wall_time_minutes",
        "create_pr",
        "pr_base",
        "pr_draft",
        "constraints",
        *_ROLES,
    ):
        if meta.get(key) is not None:
            kwargs[key] = meta[key]
    if meta.get("interactive"):
        kwargs["interactive"] = meta["interactive"]
    try:
        return RunRequest(**kwargs)
    except ValidationError as exc:
        raise HTTPException(422, detail=_validation_errors(exc)) from exc


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an http(s) URL, or None when it is not one."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname, port


# Role fields that pick the endpoint a role talks to.
_ENDPOINT_FIELDS = ("provider", "base_url")


def _default_timeout(role: RoleConfig) -> int:
    """The request timeout a role gets when none is configured (local or cloud endpoint)."""
    return LOCAL_TIMEOUT_SECONDS if is_local_model(role) else CLOUD_TIMEOUT_SECONDS


def _apply_role_override(
    role: str,
    base: RoleConfig,
    override: RoleOverride | None,
    trusted_origins: set[tuple[str, str, int]],
) -> RoleConfig:
    """Return *base* with an API caller's *override* applied.

    Server-side credentials (the configured ``api_key`` and every env key) are
    withheld when the override sends the role to a ``base_url`` the operator
    has not configured or allowed.  A role moved to another provider or
    endpoint keeps none of the old endpoint's settings, as a TOML role table
    that changes the endpoint does: its ``base_url``, credentials and prompt
    version (re-resolved, ``prompt_version=0``) are dropped and a default
    timeout becomes the new endpoint's default.  Values the override sets
    explicitly always win.
    Raises ``ValueError`` naming the role and field for a malformed
    ``base_url`` or an ``api_key_env`` that is neither the provider's default
    nor allowed by ``DELEGATRON_ALLOWED_KEY_ENVS``.
    """
    if override is None:
        return base
    fields = override.model_dump(exclude_none=True)
    if fields.get("base_url") and _url_origin(fields["base_url"]) is None:
        raise ValueError(f"{role}.base_url must be an http:// or https:// URL with a host")
    key_env = fields.get("api_key_env")
    if key_env:
        provider = fields.get("provider", base.provider)
        allowed_envs = {
            base.api_key_env,
            DEFAULT_API_KEY_ENV.get(provider, ""),
            *_env_list("DELEGATRON_ALLOWED_KEY_ENVS"),
        }
        if key_env not in allowed_envs:
            raise ValueError(
                f"{role}.api_key_env is not allowed; the server operator can permit it "
                "with DELEGATRON_ALLOWED_KEY_ENVS"
            )
    moved = any(key in fields and fields[key] != getattr(base, key) for key in _ENDPOINT_FIELDS)
    if moved:
        endpoint_reset = replace(base, base_url="", api_key="", api_key_env="", prompt_version=0)
        updated = replace(endpoint_reset, **fields)
        if "timeout_seconds" not in fields and base.timeout_seconds == _default_timeout(base):
            updated = replace(updated, timeout_seconds=_default_timeout(updated))
    else:
        updated = replace(base, **fields)
    redirected = "base_url" in fields or "provider" in fields
    if redirected and updated.base_url and _url_origin(updated.base_url) not in trusted_origins:
        updated = replace(updated, api_key="", allow_env_key=False)
    return updated


def _apply_role_overrides(
    config: Config, req: RunRequest | ReviewRequest | EstimateRequest
) -> Config:
    """Apply a request's per-role overrides to *config*; a rejected field is a 422.

    Trusted destinations are the operator's own role ``base_url``s plus
    ``DELEGATRON_ALLOWED_BASE_URLS``, compared by scheme, host and port.
    """
    urls = [getattr(config, role).base_url for role in _ROLES]
    urls += _env_list("DELEGATRON_ALLOWED_BASE_URLS")
    trusted = {origin for url in urls if url and (origin := _url_origin(url))}

    def _override(role: str) -> RoleConfig:
        return _apply_role_override(role, getattr(config, role), getattr(req, role, None), trusted)

    try:
        return replace(
            config,
            architect=_override("architect"),
            implementer=_override("implementer"),
            reviewer=_override("reviewer"),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _request_config(req: RunRequest) -> Config:
    """The operator config for *req* (preset plus optional TOML) with its role overrides."""
    config_path = (_workspace_root / req.config_path).resolve() if req.config_path else None
    root = _workspace_root.resolve()
    if config_path is not None and not config_path.is_relative_to(root):
        raise HTTPException(422, f"config_path escapes workspace: {req.config_path!r}")
    # The file is trusted as operator config (keys, endpoints): never one a run
    # wrote into its own workspace, the run artifacts or another internal directory.
    if config_path is not None and (
        is_internal_path(config_path.relative_to(root).as_posix(), root)
        or config_path.is_relative_to(_artifacts_base.resolve())
    ):
        raise HTTPException(
            422, f"config_path is inside a Delegatron directory: {req.config_path!r}"
        )
    return _apply_role_overrides(load_config(config_path=config_path, preset=req.preset), req)


async def _resolve_request_workspace(workspace: str | None, branch: str | None) -> Path:
    """Local directory for a request's workspace; a git URL is cloned off the event loop."""
    if not workspace:
        return _workspace_root
    local_path, _cleanup = await asyncio.to_thread(resolve_workspace, workspace, branch=branch)
    return local_path


_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def _git_failure_detail(workspace: str, error: Exception) -> str:
    """git's own message for a failed clone or fetch, without URL credentials."""
    output = ""
    if isinstance(error, subprocess.CalledProcessError | subprocess.TimeoutExpired):
        raw = error.stderr or error.output or ""
        output = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    if isinstance(error, subprocess.TimeoutExpired):
        output = output or f"timed out after {error.timeout:.0f}s"
    message = (output or str(error)).strip()
    return _URL_CREDENTIALS_RE.sub(r"\1", f"Could not fetch workspace {workspace}: {message}")


async def _resolve_request_workspace_or_http(workspace: str | None, branch: str | None) -> Path:
    """``_resolve_request_workspace`` for synchronous endpoints: failures become HTTP errors.

    A workspace git rejects is the caller's error (422); a clone or fetch
    that fails or times out is a bad gateway (502) carrying git's message.
    """
    try:
        return await _resolve_request_workspace(workspace, branch)
    except ValueError as e:  # InvalidWorkspaceError, or a branch name git rejects
        raise HTTPException(422, str(e)) from e
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        RepoSyncError,
    ) as e:
        logger.warning("Workspace %s could not be fetched: %s", workspace, e)
        raise HTTPException(502, _git_failure_detail(workspace or "", e)) from e


async def _start_run_internal(
    req: RunRequest,
    user: AuthUser,
    *,
    initial_state: dict[str, Any] | None = None,
    resume: _Resume | None = None,
) -> RunResponse:
    """Core run-creation logic shared by create_run, rerun, resume, template-run.

    Validates the request, persists metadata, and launches
    the pipeline in a background task.  A resume (``resume`` and the
    checkpoint ``initial_state``) runs under the existing run_id: meta.json
    keeps its settings and gains a ``resumes`` entry.
    """
    from delegatron.config import PRESETS

    # Auto-select preset based on task complexity
    if req.preset == "auto":
        from delegatron.autoselect import select_preset
        from delegatron.detection import detect_project

        ws = Path(req.workspace or _workspace_root).resolve()
        project_info = detect_project(ws) if ws.is_dir() else None
        project_data = {}
        if project_info:
            project_data = {
                "languages": project_info.languages,
                "frameworks": project_info.frameworks,
                "file_count": len(list(ws.rglob("*"))),
            }
        req = req.model_copy(update={"preset": select_preset(req.request, project_data)[0]})

    if req.preset and req.preset not in PRESETS:
        valid = ", ".join(sorted(PRESETS.keys()))
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset {req.preset!r}. Valid presets: {valid}",
        )

    if req.config_path:
        resolved = (_workspace_root / req.config_path).resolve()
        if not resolved.is_relative_to(_workspace_root.resolve()):
            raise HTTPException(
                status_code=422,
                detail=f"config_path escapes workspace: {req.config_path!r}",
            )
        if not resolved.is_file():
            raise HTTPException(
                status_code=422,
                detail=f"config_path not found: {req.config_path!r}",
            )

    # Reject disallowed role overrides before any run exists.
    try:
        _request_config(req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid config: {e}") from e

    run_id = resume.run_id if resume else uuid.uuid4().hex[:12]

    _active_streams[run_id] = []

    run_dir = _artifacts_base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if resume is not None:
        _record_resume(run_id, resume, initial_state or {})
    else:
        (run_dir / "meta.json").write_text(json.dumps(_new_run_meta(req, user)))

    task = asyncio.create_task(
        _execute_run(
            run_id,
            req,
            initial_state=initial_state,
        )
    )
    _background_tasks.add(task)
    _run_tasks[run_id] = task
    if resume is not None:
        _resumed_live.add(run_id)
        _invalidate_runs_cache()

    def _cleanup_task(t: asyncio.Task[None]) -> None:
        _background_tasks.discard(t)
        _run_tasks.pop(run_id, None)
        _resumed_live.discard(run_id)

    task.add_done_callback(_cleanup_task)

    return RunResponse(
        run_id=run_id,
        status="started",
        message=f"Pipeline started. Stream events at /api/runs/{run_id}/events",
    )


def _new_run_meta(req: RunRequest, user: AuthUser) -> dict[str, Any]:
    """meta.json of a new run: who started it and the settings retry and resume replay."""
    meta: dict[str, Any] = {
        "team_id": user.team_id,
        "user_id": user.user_id,
        "request": req.request,
        "preset": req.preset,
        "workspace": req.workspace,
        "branch": req.branch,
        "sandbox": req.sandbox,
        "max_iterations": req.max_iterations,
        "max_cost_usd": req.max_cost_usd,
        "max_wall_time_minutes": req.max_wall_time_minutes,
        "interactive": req.interactive,
        "docker_image": req.docker_image,
        "create_pr": req.create_pr,
        "pr_base": req.pr_base,
        "pr_draft": req.pr_draft,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if req.architect:
        meta["architect"] = req.architect.model_dump(exclude_defaults=True)
    if req.implementer:
        meta["implementer"] = req.implementer.model_dump(exclude_defaults=True)
    if req.reviewer:
        meta["reviewer"] = req.reviewer.model_dump(exclude_defaults=True)
    if req.constraints:
        meta["constraints"] = req.constraints.model_dump(exclude_defaults=True)
    return meta


def _record_resume(run_id: str, resume: _Resume, initial_state: Mapping[str, Any]) -> None:
    """Append this resume to meta.json ``resumes`` and to the run's event log."""
    meta = _load_run_meta(_artifacts_base / run_id)
    entry = {
        "resumed_at": datetime.now(UTC).isoformat(),
        "from_iteration": initial_state.get("iteration", 0),
        "from_status": resume.from_status,
        "overrides": resume.overrides,
    }
    _update_run_meta(run_id, resumes=[*(meta.get("resumes") or []), entry])
    with contextlib.suppress(OSError):
        ArtifactStore(base_dir=_artifacts_base, run_id=run_id).append_event(
            {"event": "run_resumed", "run_id": run_id, **entry}
        )


# --- Internal helpers ---


async def _broadcast(run_id: str, event: dict[str, Any] | None) -> None:
    """Send an event (or sentinel None) to all subscriber queues for a run."""
    queues = _active_streams.get(run_id, [])
    for q in list(queues):  # copy to avoid mutation during iteration
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer — drop oldest event to make room
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning("Dropped SSE event for run %s (slow consumer)", run_id)


def _send_alert_webhook_sync(run_id: str, status: str, request: str) -> None:
    """Send failure alert (blocking — call from to_thread)."""
    webhook_url = os.environ.get("DELEGATRON_ALERT_WEBHOOK_URL", "")
    if not webhook_url:
        return

    import urllib.request

    payload = json.dumps(
        {
            "event": "run_failed",
            "run_id": run_id,
            "status": status,
            "request": request[:200],
            "timestamp": datetime.now(UTC).isoformat(),
            "dashboard_url": f"http://localhost:3000/runs/{run_id}",
        }
    ).encode()

    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        logger.info("Alert webhook sent for run %s (%s)", run_id, status)
    except Exception as e:
        logger.warning("Alert webhook failed for run %s: %s", run_id, e)


async def _send_alert_webhook(run_id: str, status: str, request: str) -> None:
    """Send failure alert without blocking the event loop."""
    await asyncio.to_thread(_send_alert_webhook_sync, run_id, status, request)


def _init_run_workspace(base: Path, target: Path, run_id: str) -> str:
    """Create a run workspace from *base* (blocking — call from to_thread).

    The server's data, artifact and review stores are never copied, even when
    they live inside *base*. Returns the workspace's initial commit SHA.
    """
    return init_run_workspace(
        base,
        target,
        run_id,
        skip_paths=(_data_dir, _artifacts_base, _reviews_dir()),
    )


def _remove_run_workspace(run_id: str, run_workspace: Path) -> None:
    """Delete a run's workspace (blocking — call from to_thread); failures are logged."""
    try:
        remove_tree(run_workspace)
        logger.info("Removed workspace for run %s", run_id)
    except Exception:  # DELETE and cleanup must never fail on it
        logger.exception("Failed to remove workspace for run %s", run_id)


def _cleanup_cancelled_run(run_id: str, config: Any | None, run_workspace: Path | None) -> None:
    """Best-effort cleanup after a cancelled or failed run: GPU, run workspace, containers.

    The workspace of a run that saved any checkpoint is kept, including one
    with only the plan checkpoint (``state_000``): ``POST /resume`` continues
    from it, with the plan and the files of the tasks already done.  Such a
    workspace stays until ``DELETE /api/runs/{id}``.  A run that failed before
    its first checkpoint cannot be resumed, so its workspace is removed.
    """
    # 1. Unload Ollama models from GPU
    if config is not None:
        try:
            from delegatron.utils.gpu_cleanup import cleanup_after_run

            cleanup_after_run(config)
            logger.info("GPU cleanup done for cancelled run %s", run_id)
        except Exception:
            logger.exception("GPU cleanup failed for run %s", run_id)

    # 2. Remove the workspace this run used, unless the run can be resumed
    if run_workspace is not None and run_workspace.is_dir():
        if ArtifactStore(base_dir=_artifacts_base, run_id=run_id).has_state():
            logger.info("Keeping the workspace of run %s: it can be resumed", run_id)
        else:
            _remove_run_workspace(run_id, run_workspace)

    # 3. Kill any Docker containers for this run
    try:
        import subprocess as _sp

        result = _sp.run(
            ["docker", "ps", "-q", "--filter", f"label=delegatron.run_id={run_id}"],
            capture_output=True,
            text=True,
        )
        container_ids = result.stdout.strip().split()
        for cid in container_ids:
            if cid:
                _sp.run(["docker", "kill", cid], capture_output=True)
                logger.info("Killed Docker container %s for run %s", cid, run_id)
    except Exception:
        pass  # Docker may not be available


def _summary_text(run_id: str) -> str | None:
    """The run's summary.json as it is on disk, or None when it has none."""
    try:
        return (_artifacts_base / run_id / "summary.json").read_text(encoding="utf-8")
    except OSError:
        return None


def _ensure_run_summary(
    run_id: str, state: Mapping[str, Any], request: str, *, previous: str | None = None
) -> None:
    """Write a minimal summary.json when the pipeline returned without one.

    Keeps runs that ended early (e.g. a failed architect) listed and
    retryable; a summary the pipeline wrote is never overwritten.  For a
    resumed run, *previous* is the summary its earlier session left: if the
    file still holds exactly that, this session wrote none and it is replaced.
    """
    store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
    current = _summary_text(run_id)
    if current is not None and current != previous:
        return
    store.save_summary(
        {
            "run_id": run_id,
            "status": state.get("status") or "error",
            "error": state.get("error", ""),
            "total_cost_usd": _run_spend_usd(run_id, state.get("total_cost_usd") or 0.0),
            "request": request,
        }
    )


def _run_spend_usd(run_id: str, reported: float = 0.0) -> float:
    """What a run has spent across all its sessions.

    The larger of the pipeline's cumulative figure and the run's provider
    call log, which also covers sessions that were cancelled or crashed
    before they reported a total.
    """
    logged = logged_cost_usd(_artifacts_base / run_id / "provider_calls.jsonl")
    return max(float(reported or 0.0), logged)


async def _execute_run(
    run_id: str,
    req: RunRequest,
    initial_state: dict[str, Any] | None = None,
) -> None:
    """Execute a pipeline run and push events to all SSE subscribers.

    A resume (``initial_state`` is its checkpoint) continues in the run's own
    recorded workspace and run branch; a new run gets a fresh workspace.
    """
    from delegatron.orchestrator.graph import Pipeline

    # Wait for GPU/run slot — only one run at a time for local models
    if _run_semaphore.locked():
        await _broadcast(
            run_id,
            {
                "event": "queued",
                "run_id": run_id,
                "message": "Waiting for GPU — another run is in progress",
            },
        )
        logger.info("Run %s queued — waiting for GPU slot", run_id)

    acquired = False
    # A resume replaces the summary its earlier session left (see _ensure_run_summary).
    previous_summary = _summary_text(run_id) if initial_state is not None else None
    pipeline_started = False
    config = None  # Initialized before try so CancelledError handler can reference it
    run_workspace: Path | None = None  # Set once known, so cleanup removes this run's tree
    run_status = ""  # Final status, for the alert webhook
    try:
        await _run_semaphore.acquire()
        acquired = True
        # Emit started event
        await _broadcast(run_id, {"event": "started", "run_id": run_id})

        config = _request_config(req)

        # --- Pipeline overrides ---
        pipeline_overrides: dict[str, Any] = {}
        if req.max_iterations is not None:
            pipeline_overrides["max_iterations"] = req.max_iterations
        if req.max_cost_usd is not None:
            pipeline_overrides["max_cost_usd"] = req.max_cost_usd
        if req.max_wall_time_minutes is not None:
            pipeline_overrides["max_wall_time_minutes"] = req.max_wall_time_minutes
        if req.interactive is not None:
            pipeline_overrides["interactive"] = req.interactive
        if req.docker_image:
            pipeline_overrides["docker_image"] = req.docker_image
        # PR creation — wire the three fields through so the dashboard
        # can request PRs just like the CLI's --create-pr flag.
        if req.create_pr is not None:
            pipeline_overrides["create_pr"] = req.create_pr
        if req.pr_base is not None:
            pipeline_overrides["pr_base"] = req.pr_base
        if req.pr_draft is not None:
            pipeline_overrides["pr_draft"] = req.pr_draft
        if req.constraints:
            # Request constraints add to (never replace) those from the config TOML.
            pipeline_overrides["constraints"] = merge_constraints(
                config.pipeline.constraints, req.constraints.model_dump(exclude_defaults=True)
            )
        if pipeline_overrides:
            config = replace(
                config,
                pipeline=replace(config.pipeline, **pipeline_overrides),
            )

        # Build a step_callback that persists pipeline milestones to
        # events.jsonl AND broadcasts them to live SSE subscribers.
        # Persisting ensures late-connecting clients see all events.
        store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)

        def _step_cb(step: str, data: dict[str, Any]) -> None:
            evt = {"event": step, **data}
            # Persist non-token events to disk so late-connecting
            # SSE clients see them during replay.
            if step != "token":
                with contextlib.suppress(Exception):
                    store.append_event(evt)
                    # evt now has a timestamp from append_event —
                    # broadcast the same dict so dedup can match.
            # Broadcast to live SSE subscribers
            try:
                loop = asyncio.get_running_loop()
                _task = loop.create_task(_broadcast(run_id, evt))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)
            except RuntimeError:
                pass  # No running loop — nothing to broadcast to

        # When running inside the server, never call input().  Override
        # Interactive mode: use an async approval callback that waits
        # for the dashboard user to approve/reject the plan via API.
        approval_cb = None
        if config.pipeline.interactive:
            import asyncio as _aio

            _approval_timeout_minutes = float(
                os.environ.get("DELEGATRON_APPROVAL_TIMEOUT_MINUTES", "30")
            )

            async def _async_approval(plan: dict[str, Any]) -> bool:
                evt = _aio.Event()
                _pending_approvals[run_id] = {"plan": plan, "event": evt, "approved": False}
                await _broadcast(run_id, {"event": "plan_pending_approval", "plan": plan})
                logger.info("Waiting for plan approval from dashboard (run %s)", run_id)
                timeout_seconds = _approval_timeout_minutes * 60
                try:
                    await asyncio.wait_for(evt.wait(), timeout=timeout_seconds)
                except TimeoutError:
                    logger.warning(
                        "Plan approval timed out for run %s — auto-rejecting",
                        run_id,
                    )
                    _pending_approvals.pop(run_id, None)
                    return False
                result = _pending_approvals.pop(run_id, {})
                return cast(bool, result.get("approved", False))

            approval_cb = _async_approval

        if initial_state is not None:
            # A resume continues in the run's own workspace, on its run branch.
            run_workspace = recorded_run_workspace(run_id, _load_run_meta(_artifacts_base / run_id))
            if run_workspace is None or not (run_workspace / ".git").exists():
                raise WorkspaceInitError(f"The workspace of run {run_id} no longer exists")
        else:
            # Each run works in its own copy of the workspace, so concurrent or
            # sequential runs on the same workspace never collide.
            base_workspace = await _resolve_request_workspace(req.workspace, req.branch)

            # Prevent using the Delegatron installation as a workspace
            if _is_install_dir(base_workspace):
                raise ValueError(
                    f"Workspace '{base_workspace}' is the Delegatron installation directory. "
                    f"Use a different directory (e.g. {Path.home() / 'delegatron-workspace'})."
                )
            run_workspace = base_workspace / RUNS_DIR_NAME / run_id
            if not run_workspace.exists():
                init_sha = await asyncio.to_thread(
                    _init_run_workspace, base_workspace, run_workspace, run_id
                )
                # Recorded for downloads, restore, resume and cleanup.
                _update_run_meta(
                    run_id,
                    run_workspace=str(run_workspace.resolve()),
                    workspace_init_sha=init_sha,
                )

        # Override sandbox if specified
        if req.sandbox and req.sandbox != config.pipeline.sandbox:
            config = replace(config, pipeline=replace(config.pipeline, sandbox=req.sandbox))

        pipeline = Pipeline(
            config=config,
            workspace_root=run_workspace,
            run_id=run_id,
            artifacts_dir=_artifacts_base,
            step_callback=_step_cb,
            # _step_cb writes every step to events.jsonl itself.
            step_callback_persists=True,
            approval_callback=approval_cb,
        )

        pipeline_started = True
        await _broadcast(run_id, {"event": "pipeline_executing", "run_id": run_id})
        # Enable streaming so token events are broadcast via SSE to the
        # dashboard's Live Thinking panel.
        state = await pipeline.execute(req.request, initial_state=initial_state, stream=True)

        run_status = state.get("status", "")
        _ensure_run_summary(run_id, state, req.request, previous=previous_summary)
        # The outcomes the pipeline's zero-cloud report counts as alerted.
        from delegatron.orchestrator.graph import ALERT_STATUSES

        if run_status in ALERT_STATUSES:
            await _send_alert_webhook(run_id, run_status, req.request)

        # Push final state to stream
        serialized = _serialize_state(cast(dict[str, Any], state))
        await _broadcast(run_id, {"event": "complete", "state": serialized})
        _invalidate_runs_cache()

    except asyncio.CancelledError:
        logger.warning("Pipeline run %s was cancelled", run_id)
        run_status = "cancelled"
        await _broadcast(run_id, {"event": "error", "error": "Run cancelled"})

        # Persist a cancelled summary
        try:
            store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
            store.save_summary(
                {
                    "run_id": run_id,
                    "status": "cancelled",
                    "error": "Run was cancelled",
                    "total_cost_usd": _run_spend_usd(run_id),
                }
            )
        except Exception:
            logger.exception("Failed to persist cancel summary for run %s", run_id)

        # Cleanup GPU, workspace, and Docker containers for cancelled runs
        await asyncio.to_thread(
            _cleanup_cancelled_run,
            run_id,
            config if pipeline_started else None,
            run_workspace,
        )

        # Send failure alert webhook for cancelled runs
        await _send_alert_webhook(run_id, "cancelled", req.request)

    except Exception as e:
        logger.exception("Pipeline run %s failed", run_id)
        run_status = "error"
        await _broadcast(run_id, {"event": "error", "error": str(e)})

        # Persist a failed summary so the run doesn't appear as a zombie
        try:
            store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
            store.save_summary(
                {
                    "run_id": run_id,
                    "status": "error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "total_cost_usd": _run_spend_usd(run_id),
                }
            )
        except Exception:
            logger.exception("Failed to persist error summary for run %s", run_id)

        # Send failure alert webhook if configured
        if run_status in ("error", "cancelled", "exhausted"):
            await _send_alert_webhook(run_id, run_status, req.request)

        # Cleanup GPU, workspace, and Docker containers for failed runs
        await asyncio.to_thread(
            _cleanup_cancelled_run,
            run_id,
            config if pipeline_started else None,
            run_workspace,
        )

    finally:
        # Clean up pending approval if the run ends while waiting for approval
        _pending_approvals.pop(run_id, None)

        # Release GPU slot (only if we acquired it)
        if acquired:
            _run_semaphore.release()
        else:
            # Cancelled while queued — write a cancelled summary
            try:
                store = ArtifactStore(base_dir=_artifacts_base, run_id=run_id)
                store.save_summary(
                    {
                        "run_id": run_id,
                        "status": "cancelled",
                        "error": "Cancelled while waiting for GPU slot",
                        "total_cost_usd": _run_spend_usd(run_id),
                    }
                )
            except Exception:
                logger.warning("Failed to save cancelled summary for queued run %s", run_id)

        # This session's outcome is in summary.json now.
        if run_id in _resumed_live:
            _resumed_live.discard(run_id)
            _invalidate_runs_cache()

        # Signal end of stream
        await _broadcast(run_id, None)
        # Clean up after a delay (allow clients to read final events)
        await asyncio.sleep(5)
        _active_streams.pop(run_id, None)


async def _sse_from_queue(
    queue: asyncio.Queue[dict[str, Any] | None],
    run_id: str,
    queues: list[asyncio.Queue[dict[str, Any] | None]] | None = None,
) -> Any:
    """Generate SSE events from an asyncio queue.

    Removes the queue from the subscriber list on disconnect so dead
    subscribers don't leak memory.
    """
    try:
        while True:
            event = await queue.get()
            if event is None:
                yield f"event: done\ndata: {json.dumps({'run_id': run_id})}\n\n"
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        # Clean up subscriber on disconnect or completion
        if queues is not None:
            with contextlib.suppress(ValueError):
                queues.remove(queue)


async def _sse_replay_then_live(
    events_file: Path,
    queue: asyncio.Queue[dict[str, Any] | None],
    run_id: str,
    queues: list[asyncio.Queue[dict[str, Any] | None]] | None = None,
) -> Any:
    """Replay saved events from disk, then stream live events from queue."""
    try:
        # Phase 1: replay events already saved to disk.
        # Collect the raw JSON lines so we can deduplicate them against
        # queue events that arrived between queue creation and file read.
        replayed_lines: set[str] = set()
        if events_file.exists():
            with open(events_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        replayed_lines.add(line)
                        yield f"data: {line}\n\n"
        # Phase 2: stream live events from queue.
        # Skip events we already replayed from disk (content-based dedup).
        # Events that only exist on the queue (e.g. step_callback milestones)
        # are never skipped. Once all replayed duplicates have been seen,
        # stop checking to avoid O(n) set lookups for the rest of the run.
        remaining_dedup = len(replayed_lines)
        async for chunk in _sse_from_queue(queue, run_id, queues):
            if remaining_dedup > 0 and "event: done" not in chunk:
                # Extract the JSON payload from the SSE chunk
                payload = chunk.removeprefix("data: ").rstrip("\n")
                if payload in replayed_lines:
                    replayed_lines.discard(payload)
                    remaining_dedup -= 1
                    continue
            yield chunk
    finally:
        # Ensure queue is removed even if client disconnects during replay
        if queues is not None:
            with contextlib.suppress(ValueError):
                queues.remove(queue)


async def _sse_from_file(events_file: Path) -> Any:
    """Generate SSE events from a saved JSONL file."""
    with open(events_file) as f:
        for line in f:
            line = line.strip()
            if line:
                yield f"data: {line}\n\n"
    yield f"event: done\ndata: {json.dumps({'source': 'replay'})}\n\n"


def _serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Serialize pipeline state for JSON response (strip non-serializable)."""
    safe = {}
    for key, value in state.items():
        try:
            json.dumps(value, default=str)
            safe[key] = value
        except (TypeError, ValueError):
            safe[key] = str(value)
    return safe


# Dashboard is served by `next start` subprocess, spawned from CLI serve command.
# No static file mounting needed — the API server only serves /api/* endpoints.
