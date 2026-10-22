"""Token and cost tracking with provider call logging.

Includes local GPU cost estimation for self-hosted models (Ollama).
Local models have zero API cost, but consume electricity and wear GPU
hardware. This module estimates those costs from nvidia-smi power draw
and configurable electricity rates.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local GPU cost estimation
# ---------------------------------------------------------------------------

# Default US average electricity rate ($/kWh) — override with env var
_DEFAULT_ELECTRICITY_RATE = 0.12

# Fallback GPU TDP in watts when nvidia-smi is unavailable
_DEFAULT_GPU_TDP_WATTS = 250.0

# Optional hardware amortization: GPU purchase price / expected lifetime hours
# Default: $1,500 GPU / 20,000 hours ≈ $0.075/hour
_DEFAULT_GPU_PRICE_USD = 1500.0
_DEFAULT_GPU_LIFETIME_HOURS = 20_000.0


def get_gpu_power_watts() -> float | None:
    """Query nvidia-smi for current GPU power draw in watts.

    Returns None if nvidia-smi is unavailable or fails.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            # May have multiple GPUs — take the first (or max)
            values = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        values.append(float(line))
                    except ValueError:
                        continue
            if values:
                return max(values)  # Use the highest-drawing GPU
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def estimate_local_cost(
    generation_seconds: float,
    gpu_watts: float | None = None,
) -> dict[str, float]:
    """Estimate the real cost of running a local model.

    Returns a breakdown with electricity cost, hardware amortization,
    and total. All values in USD.
    """
    electricity_rate = float(
        os.environ.get("DELEGATRON_ELECTRICITY_RATE", _DEFAULT_ELECTRICITY_RATE)
    )
    gpu_price = float(os.environ.get("DELEGATRON_GPU_PRICE_USD", _DEFAULT_GPU_PRICE_USD))
    gpu_lifetime = float(
        os.environ.get("DELEGATRON_GPU_LIFETIME_HOURS", _DEFAULT_GPU_LIFETIME_HOURS)
    )
    tdp_fallback = float(os.environ.get("DELEGATRON_GPU_TDP_WATTS", _DEFAULT_GPU_TDP_WATTS))

    watts = gpu_watts if gpu_watts is not None else tdp_fallback
    hours = generation_seconds / 3600.0

    # Electricity: (kW) * hours * $/kWh
    electricity_cost = (watts / 1000.0) * hours * electricity_rate

    # Hardware wear: amortized cost per hour of GPU use
    hardware_cost = (gpu_price / gpu_lifetime) * hours

    return {
        "electricity_usd": round(electricity_cost, 6),
        "hardware_usd": round(hardware_cost, 6),
        "total_local_usd": round(electricity_cost + hardware_cost, 6),
        "gpu_watts": round(watts, 1),
        "generation_seconds": round(generation_seconds, 2),
        "electricity_rate_kwh": electricity_rate,
    }


# Approximate pricing per million tokens (USD) as of 2026-10.  Optional
# ``cached_input`` (prompt-cache reads) and ``cache_write`` (prompt-cache
# writes) rates fall back to ``input``.
# Keys include both bare model names and LiteLLM-prefixed variants
# so cost tracking works regardless of which provider path is used.
_SONNET_5_5 = {"input": 2.0, "output": 10.0, "cached_input": 0.20, "cache_write": 2.50}
# Retired 2026-06-15; kept so saved runs still price.
_SONNET_4 = {"input": 3.0, "output": 15.0, "cached_input": 0.30, "cache_write": 3.75}
_OPUS_4_6 = {"input": 15.0, "output": 75.0, "cached_input": 1.50, "cache_write": 18.75}
_GEMINI_25_PRO = {"input": 1.25, "output": 10.0, "cached_input": 0.31}
_GEMINI_25_FLASH = {"input": 0.30, "output": 2.50, "cached_input": 0.075}
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-5-5": _SONNET_5_5,
    "anthropic/claude-sonnet-5-5": _SONNET_5_5,
    "claude-sonnet-4-20250514": _SONNET_4,
    "anthropic/claude-sonnet-4-20250514": _SONNET_4,
    "claude-opus-4-6": _OPUS_4_6,
    "anthropic/claude-opus-4-6": _OPUS_4_6,
    # OpenAI
    "gpt-4.1": {"input": 2.0, "output": 8.0, "cached_input": 0.50},
    # Google
    "gemini-2.5-pro": _GEMINI_25_PRO,
    "gemini/gemini-2.5-pro": _GEMINI_25_PRO,
    "gemini-2.5-flash": _GEMINI_25_FLASH,
    "gemini/gemini-2.5-flash": _GEMINI_25_FLASH,
}

# Rates assumed for a cloud model missing from ``PRICING``.
FALLBACK_CLOUD_PRICING: dict[str, float] = {"input": 5.0, "output": 15.0}
_LOCAL_PRICING: dict[str, float] = {"input": 0.0, "output": 0.0}
# Unpriced cloud models already warned about (one warning per model and process).
_warned_unpriced: set[str] = set()
_warned_lock = threading.Lock()

# Patterns that indicate a local model (cost = $0) when the caller does not
# say where the model runs.
_LOCAL_MODEL_PATTERNS = (
    "gpt-oss",
    "qwen",
    "deepseek",
    "llama",
    "codellama",
    "mistral",
    "mixtral",
    "phi",
    "starcoder",
    "codestral",
    "gemma",
    "codegemma",
    "yi-coder",
    "granite",
    "wizard",
    "nous-hermes",
    "solar",
    "orca",
    "vicuna",
)


def _is_local_model(model: str) -> bool:
    """Check if a model name looks like a local/Ollama model (free)."""
    name = model.lower()
    if name.startswith("mock"):
        return True
    return any(p in name for p in _LOCAL_MODEL_PATTERNS)


def logged_cost_usd(log_file: Path) -> float:
    """Sum ``cost_usd`` over a ``provider_calls.jsonl`` log (0.0 when absent).

    The log is appended across sessions of the same run, so this recovers the
    spend of calls made after the last state checkpoint.
    """
    total = 0.0
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    for line in lines:
        try:
            total += float(json.loads(line).get("cost_usd", 0.0))
        except (ValueError, TypeError, AttributeError):
            continue
    return total


def _normalize_model_name(model: str) -> str:
    """Strip LiteLLM provider prefix for pricing lookup fallback.

    LiteLLM uses prefixed model strings like "anthropic/claude-sonnet-5-5".
    This strips the prefix so we can match against bare model names too.
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _pricing(model: str, is_local: bool) -> dict[str, float]:
    """Per-million-token rates of *model*; a fallback for local or unpriced models.

    An unpriced cloud model is assumed to cost ``FALLBACK_CLOUD_PRICING`` and
    logs one warning per model, since that guess can end a run on its cost cap.
    """
    known = PRICING.get(model) or PRICING.get(_normalize_model_name(model))
    if known:
        return known
    if is_local:
        return _LOCAL_PRICING
    with _warned_lock:
        first = model not in _warned_unpriced
        _warned_unpriced.add(model)
    if first:
        logger.warning(
            "No price known for model %r: assuming $%.2f/$%.2f per million input/output "
            "tokens; the run's cost (and the max_cost_usd limit) is based on that guess",
            model,
            FALLBACK_CLOUD_PRICING["input"],
            FALLBACK_CLOUD_PRICING["output"],
        )
    return FALLBACK_CLOUD_PRICING


def _api_cost(
    pricing: dict[str, float],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD for one call; *input_tokens* includes the cache reads and writes."""
    uncached = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    return (
        uncached * pricing["input"]
        + cache_read_tokens * pricing.get("cached_input", pricing["input"])
        + cache_write_tokens * pricing.get("cache_write", pricing["input"])
        + output_tokens * pricing["output"]
    ) / 1_000_000


@dataclass
class CallRecord:
    """Record of a single provider API call."""

    request_id: str
    provider_request_id: str
    model: str
    role: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str = ""
    iteration: int = 0
    is_local: bool = False
    local_cost: dict[str, float] | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "request_id": self.request_id,
            "provider_request_id": self.provider_request_id,
            "model": self.model,
            "role": self.role,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "timestamp": self.timestamp,
            "iteration": self.iteration,
            "is_local": self.is_local,
        }
        if self.cache_read_tokens:
            d["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens:
            d["cache_write_tokens"] = self.cache_write_tokens
        if self.temperature is not None:
            d["temperature"] = self.temperature
        if self.local_cost:
            d["local_cost"] = self.local_cost
        return d


@dataclass
class CostTracker:
    """Tracks cumulative token usage and estimated costs across a run.

    For local models (Ollama), API cost is $0 but we estimate real costs
    from GPU power consumption and electricity rates. Pass
    ``generation_seconds`` and optionally ``gpu_watts`` to ``record()``
    for local models to get meaningful cost data.

    ``total_cost_usd`` covers this session's calls; ``prior_cost_usd`` is the
    spend carried over from earlier sessions of the same run (resume, refine).
    """

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cost_usd: float = 0.0
    total_local_cost_usd: float = 0.0
    prior_cost_usd: float = 0.0
    calls: list[CallRecord] = field(default_factory=list)
    _log_file: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cumulative_cost_usd(self) -> float:
        """Spend of the whole run: earlier sessions plus this one."""
        return self.prior_cost_usd + self.total_cost_usd

    def set_log_file(self, path: Path) -> None:
        """Set a JSONL file to append call records to."""
        self._log_file = path

    def usage_mark(self) -> tuple[int, int, int]:
        """``(total_input_tokens, total_output_tokens, calls)``, read together.

        Two marks bracket a step: their difference is exactly what the step
        booked, also while parallel tasks record concurrently.
        """
        with self._lock:
            return self.total_input_tokens, self.total_output_tokens, len(self.calls)

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        request_id: str = "",
        provider_request_id: str = "",
        role: str = "",
        iteration: int = 0,
        generation_seconds: float = 0.0,
        gpu_watts: float | None = None,
        is_local: bool | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        temperature: float | None = None,
    ) -> float:
        """Record a single API call and return its estimated cost.

        For local models, pass ``generation_seconds`` (wall-clock time the
        GPU was active) to get an electricity + hardware amortization
        estimate instead of $0.  ``is_local`` (from the role's endpoint)
        overrides the guess made from the model name.  A model with an API
        price (``PRICING``) reached through a local endpoint is a proxy (e.g.
        LiteLLM on localhost) in front of the cloud API, so it is billed at
        that price, not estimated from the GPU.  ``input_tokens`` is
        the whole prompt; the parts read from and written to the provider's
        prompt cache (``cache_read_tokens``, ``cache_write_tokens``) are priced
        at the model's cache rates.
        """
        cache_read_tokens = max(0, cache_read_tokens or 0)
        cache_write_tokens = max(0, cache_write_tokens or 0)
        input_tokens = max(0, input_tokens, cache_read_tokens + cache_write_tokens)
        output_tokens = max(0, output_tokens)
        if is_local is None:
            is_local = _is_local_model(model)
        pricing = _pricing(model, is_local)

        api_cost = _api_cost(
            pricing, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )

        # For local models, estimate real costs from GPU power
        local_cost_data: dict[str, float] | None = None
        effective_cost = api_cost
        proxied = bool(PRICING.get(model) or PRICING.get(_normalize_model_name(model)))
        if is_local and generation_seconds > 0 and not proxied:
            local_cost_data = estimate_local_cost(generation_seconds, gpu_watts)
            effective_cost = local_cost_data["total_local_usd"]

        record = CallRecord(
            request_id=request_id,
            provider_request_id=provider_request_id,
            model=model,
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=effective_cost,
            iteration=iteration,
            is_local=is_local,
            local_cost=local_cost_data,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            temperature=temperature,
        )

        # Thread-safe accumulation (safe for parallel fan-out tasks)
        with self._lock:
            if local_cost_data is not None:
                self.total_local_cost_usd += effective_cost
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.total_cache_read_tokens += cache_read_tokens
            self.total_cache_write_tokens += cache_write_tokens
            self.total_cost_usd += effective_cost
            self.calls.append(record)

            # Append to JSONL log file while still holding the lock
            if self._log_file:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record.to_dict(), default=str) + "\n")

        return effective_cost

    def estimate_cost(
        self, model: str, input_tokens: int, output_tokens: int, is_local: bool | None = None
    ) -> float:
        """Estimate cost for a hypothetical call without recording it."""
        pricing = _pricing(model, _is_local_model(model) if is_local is None else is_local)
        return _api_cost(pricing, input_tokens, output_tokens)

    def get_summary(self) -> dict[str, Any]:
        """Return a summary of all calls, including local cost breakdown.

        ``total_cost_usd`` is cumulative across sessions; ``by_role`` and the
        token counts cover this session (``session_cost_usd``) only.
        """
        by_role: dict[str, dict[str, Any]] = {}
        total_electricity = 0.0
        total_hardware = 0.0
        total_gen_seconds = 0.0
        has_local = False

        for call in self.calls:
            role = call.role or "unknown"
            if role not in by_role:
                by_role[role] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_usd": 0.0,
                    "is_local": False,
                }
            by_role[role]["calls"] += 1
            by_role[role]["input_tokens"] += call.input_tokens
            by_role[role]["output_tokens"] += call.output_tokens
            by_role[role]["cache_read_tokens"] += call.cache_read_tokens
            by_role[role]["cache_write_tokens"] += call.cache_write_tokens
            by_role[role]["cost_usd"] += call.cost_usd

            if call.is_local:
                has_local = True
                by_role[role]["is_local"] = True
                if call.local_cost:
                    total_electricity += call.local_cost["electricity_usd"]
                    total_hardware += call.local_cost["hardware_usd"]
                    total_gen_seconds += call.local_cost["generation_seconds"]

        result: dict[str, Any] = {
            "total_calls": len(self.calls),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_cost_usd": self.cumulative_cost_usd,
            "prior_cost_usd": self.prior_cost_usd,
            "session_cost_usd": self.total_cost_usd,
            "by_role": by_role,
        }

        if has_local:
            result["local_cost"] = {
                "total_local_usd": round(self.total_local_cost_usd, 6),
                "electricity_usd": round(total_electricity, 6),
                "hardware_usd": round(total_hardware, 6),
                "generation_seconds": round(total_gen_seconds, 2),
            }

        return result
