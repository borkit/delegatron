"""Rate limiting — sliding-window in-memory rate limiter."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    max_runs_per_hour: int = 10
    max_cost_per_hour_usd: float = 50.0
    max_concurrent_runs: int = 3


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    reason: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class _UserState:
    """Internal per-user tracking state."""

    run_timestamps: list[float] = field(default_factory=list)
    cost_entries: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, cost)
    active_runs: set[str] = field(default_factory=set)


class RateLimiter:
    """In-memory sliding-window rate limiter.

    Tracks per-user run counts, costs, and concurrent runs using
    a one-hour sliding window. Thread-safe.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._users: dict[str, _UserState] = {}
        self._lock = threading.Lock()

    def _get_user(self, user_id: str) -> _UserState:
        if user_id not in self._users:
            self._users[user_id] = _UserState()
        return self._users[user_id]

    def _prune(self, state: _UserState, now: float) -> None:
        """Remove entries older than one hour."""
        cutoff = now - 3600.0
        state.run_timestamps = [t for t in state.run_timestamps if t > cutoff]
        state.cost_entries = [(t, c) for t, c in state.cost_entries if t > cutoff]

    def check(self, user_id: str) -> RateLimitResult:
        """Check whether a new run is allowed for the given user."""
        with self._lock:
            now = time.monotonic()
            state = self._get_user(user_id)
            self._prune(state, now)

            # Check concurrent runs
            if len(state.active_runs) >= self._config.max_concurrent_runs:
                return RateLimitResult(
                    allowed=False,
                    reason=(f"Max concurrent runs ({self._config.max_concurrent_runs}) reached"),
                    retry_after_seconds=60,
                )

            # Check runs per hour
            if len(state.run_timestamps) >= self._config.max_runs_per_hour and state.run_timestamps:
                oldest = state.run_timestamps[0]
                retry_after = int(oldest + 3600.0 - now) + 1
                return RateLimitResult(
                    allowed=False,
                    reason=(f"Max runs per hour ({self._config.max_runs_per_hour}) reached"),
                    retry_after_seconds=max(1, retry_after),
                )

            # Check cost per hour
            total_cost = sum(c for _, c in state.cost_entries)
            if total_cost >= self._config.max_cost_per_hour_usd:
                if state.cost_entries:
                    oldest_cost_ts = state.cost_entries[0][0]
                    retry_after = int(oldest_cost_ts + 3600.0 - now) + 1
                else:
                    retry_after = 3600
                return RateLimitResult(
                    allowed=False,
                    reason=(
                        f"Max cost per hour (${self._config.max_cost_per_hour_usd:.2f}) reached"
                    ),
                    retry_after_seconds=max(1, retry_after),
                )

            return RateLimitResult(allowed=True)

    def record_start(self, user_id: str, run_id: str) -> None:
        """Record that a run has started."""
        with self._lock:
            now = time.monotonic()
            state = self._get_user(user_id)
            state.run_timestamps.append(now)
            state.active_runs.add(run_id)

    def record_end(self, user_id: str, run_id: str, cost: float) -> None:
        """Record that a run has ended with a given cost."""
        with self._lock:
            now = time.monotonic()
            state = self._get_user(user_id)
            state.active_runs.discard(run_id)
            state.cost_entries.append((now, cost))
