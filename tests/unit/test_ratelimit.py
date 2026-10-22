"""Tests for the rate limiting module."""

from __future__ import annotations

from unittest.mock import patch

from delegatron.ratelimit import RateLimitConfig, RateLimiter, RateLimitResult


class TestRateLimitResult:
    def test_allowed(self) -> None:
        r = RateLimitResult(allowed=True)
        assert r.allowed is True
        assert r.reason is None
        assert r.retry_after_seconds is None

    def test_denied(self) -> None:
        r = RateLimitResult(allowed=False, reason="too many", retry_after_seconds=60)
        assert r.allowed is False
        assert r.reason == "too many"


class TestRateLimitConfig:
    def test_defaults(self) -> None:
        cfg = RateLimitConfig()
        assert cfg.max_runs_per_hour == 10
        assert cfg.max_cost_per_hour_usd == 50.0
        assert cfg.max_concurrent_runs == 3


class TestRateLimiter:
    def test_allows_first_request(self) -> None:
        limiter = RateLimiter(RateLimitConfig())
        result = limiter.check("user1")
        assert result.allowed is True

    def test_concurrent_run_limit(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_concurrent_runs=2))

        limiter.record_start("user1", "run-1")
        limiter.record_start("user1", "run-2")

        result = limiter.check("user1")
        assert result.allowed is False
        assert "concurrent" in (result.reason or "").lower()

        # Ending a run should free a slot
        limiter.record_end("user1", "run-1", cost=1.0)
        result = limiter.check("user1")
        assert result.allowed is True

    def test_runs_per_hour_limit(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_runs_per_hour=3))

        for i in range(3):
            limiter.record_start("user1", f"run-{i}")
            limiter.record_end("user1", f"run-{i}", cost=0.5)

        result = limiter.check("user1")
        assert result.allowed is False
        assert "runs per hour" in (result.reason or "").lower()
        assert result.retry_after_seconds is not None
        assert result.retry_after_seconds > 0

    def test_cost_per_hour_limit(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_cost_per_hour_usd=5.0))

        for i in range(3):
            limiter.record_start("user1", f"run-{i}")
            limiter.record_end("user1", f"run-{i}", cost=2.0)

        result = limiter.check("user1")
        assert result.allowed is False
        assert "cost" in (result.reason or "").lower()
        assert result.retry_after_seconds is not None

    def test_users_are_independent(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_runs_per_hour=2))

        for i in range(2):
            limiter.record_start("alice", f"run-{i}")
            limiter.record_end("alice", f"run-{i}", cost=0.5)

        # Alice is at limit
        assert limiter.check("alice").allowed is False
        # Bob is fine
        assert limiter.check("bob").allowed is True

    def test_sliding_window_expiry(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_runs_per_hour=2))

        # Simulate old timestamps by patching time.monotonic
        base_time = 1000.0
        with patch("delegatron.ratelimit.time.monotonic", return_value=base_time):
            limiter.record_start("user1", "run-1")
            limiter.record_end("user1", "run-1", cost=1.0)
            limiter.record_start("user1", "run-2")
            limiter.record_end("user1", "run-2", cost=1.0)

        # Now advance past the 1-hour window
        future_time = base_time + 3601.0
        with patch("delegatron.ratelimit.time.monotonic", return_value=future_time):
            result = limiter.check("user1")
            assert result.allowed is True

    def test_record_end_idempotent(self) -> None:
        limiter = RateLimiter(RateLimitConfig())
        limiter.record_start("user1", "run-1")
        limiter.record_end("user1", "run-1", cost=1.0)
        # Ending again should not raise
        limiter.record_end("user1", "run-1", cost=0.0)

    def test_cost_accumulates_within_window(self) -> None:
        limiter = RateLimiter(RateLimitConfig(max_cost_per_hour_usd=10.0))

        for i in range(5):
            limiter.record_start("user1", f"run-{i}")
            limiter.record_end("user1", f"run-{i}", cost=1.5)

        # 5 * 1.5 = 7.5, still under 10.0
        result = limiter.check("user1")
        assert result.allowed is True

        # One more pushes to 9.0, still under
        limiter.record_start("user1", "run-5")
        limiter.record_end("user1", "run-5", cost=1.5)

        # 6 * 1.5 = 9.0, still under
        result = limiter.check("user1")
        assert result.allowed is True

        # Push over
        limiter.record_start("user1", "run-6")
        limiter.record_end("user1", "run-6", cost=2.0)

        # 9.0 + 2.0 = 11.0, over limit
        result = limiter.check("user1")
        assert result.allowed is False
