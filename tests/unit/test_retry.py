"""Tests for utils/retry.py — retry_with_backoff."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from delegatron.agents.providers.base import (
    ProviderError,
    ProviderRequestError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from delegatron.utils.retry import is_retryable, retry_with_backoff


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        fn = AsyncMock(return_value="ok")
        result = await retry_with_backoff(fn, max_retries=3)
        assert result == "ok"
        fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_then_succeeds(self):
        fn = AsyncMock(side_effect=[RuntimeError("transient"), "ok"])
        with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert fn.await_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_max_retries(self):
        fn = AsyncMock(side_effect=RuntimeError("always fails"))
        with (
            patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(RuntimeError, match="always fails"),
        ):
            await retry_with_backoff(fn, max_retries=2, base_delay=0.01)
        # 1 initial + 2 retries = 3 total calls
        assert fn.await_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_json_decode_error(self):
        """JSONDecodeError should not be retried — re-raised immediately."""
        fn = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
        with pytest.raises(json.JSONDecodeError):
            await retry_with_backoff(fn, max_retries=3)
        fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_retryable_value_error(self):
        """ValueError should not be retried."""
        fn = AsyncMock(side_effect=ValueError("bad value"))
        with pytest.raises(ValueError, match="bad value"):
            await retry_with_backoff(fn, max_retries=3)
        fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_retryable_validation_error(self):
        """Pydantic ValidationError should not be retried."""
        from pydantic import BaseModel, ValidationError

        class M(BaseModel):
            x: int

        with pytest.raises(ValidationError) as caught:
            M(x="not_int")  # type: ignore[arg-type]

        fn = AsyncMock(side_effect=caught.value)
        with pytest.raises(ValidationError):
            await retry_with_backoff(fn, max_retries=3)
        fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delay_capped_at_max_delay(self):
        """Delay should never exceed max_delay."""
        fn = AsyncMock(side_effect=[RuntimeError("e1"), RuntimeError("e2"), "ok"])
        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        with patch("delegatron.utils.retry.asyncio.sleep", side_effect=fake_sleep):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=100.0, max_delay=5.0)
        assert result == "ok"
        assert len(sleep_calls) == 2
        for delay in sleep_calls:
            # max_delay=5.0 + 10% jitter max = 5.5
            assert delay <= 5.5

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self):
        fn = AsyncMock(return_value="result")
        await retry_with_backoff(fn, "arg1", "arg2", max_retries=1, kw1="v1")
        fn.assert_awaited_once_with("arg1", "arg2", kw1="v1")

    @pytest.mark.asyncio
    async def test_zero_retries(self):
        """With max_retries=0, should call once then raise."""
        fn = AsyncMock(side_effect=RuntimeError("once"))
        with pytest.raises(RuntimeError, match="once"):
            await retry_with_backoff(fn, max_retries=0)
        fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jitter_is_applied(self):
        """Verify that jitter causes delay variation."""
        fn = AsyncMock(
            side_effect=[
                RuntimeError("e1"),
                RuntimeError("e2"),
                RuntimeError("e3"),
                "ok",
            ]
        )
        sleep_values: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_values.append(t)

        with (
            patch("delegatron.utils.retry.asyncio.sleep", side_effect=fake_sleep),
            patch("delegatron.utils.retry.random.uniform", return_value=0.05),
        ):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=1.0)
        assert result == "ok"
        # With base_delay=1.0 and jitter=0.05:
        # attempt 0: delay = min(1*1, 60) + 0.05 = 1.05
        # attempt 1: delay = min(1*2, 60) + 0.05 = 2.05
        # attempt 2: delay = min(1*4, 60) + 0.05 = 4.05
        assert len(sleep_values) == 3
        assert abs(sleep_values[0] - 1.05) < 0.01
        assert abs(sleep_values[1] - 2.05) < 0.01
        assert abs(sleep_values[2] - 4.05) < 0.01

    @pytest.mark.asyncio
    async def test_exponential_backoff_progression(self):
        """Verify delay doubles on each attempt."""
        fn = AsyncMock(side_effect=[RuntimeError("e1"), RuntimeError("e2"), "ok"])
        sleep_values: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_values.append(t)

        with (
            patch("delegatron.utils.retry.asyncio.sleep", side_effect=fake_sleep),
            patch("delegatron.utils.retry.random.uniform", return_value=0.0),
        ):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=2.0, max_delay=60.0)
        assert result == "ok"
        assert len(sleep_values) == 2
        # attempt 0: 2 * 2^0 = 2.0
        # attempt 1: 2 * 2^1 = 4.0
        assert abs(sleep_values[0] - 2.0) < 0.01
        assert abs(sleep_values[1] - 4.0) < 0.01

    @pytest.mark.asyncio
    async def test_multiple_retries_then_succeeds(self):
        """Function fails multiple times then succeeds on last retry."""
        fn = AsyncMock(
            side_effect=[
                RuntimeError("e1"),
                RuntimeError("e2"),
                RuntimeError("e3"),
                "finally",
            ]
        )
        with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        assert result == "finally"
        assert fn.await_count == 4

    @pytest.mark.asyncio
    async def test_retryable_os_error(self):
        """OSError should be retried (not in non-retryable list)."""
        fn = AsyncMock(side_effect=[OSError("connection reset"), "recovered"])
        with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        assert result == "recovered"
        assert fn.await_count == 2

    @pytest.mark.asyncio
    async def test_retryable_timeout_error(self):
        """TimeoutError should be retried."""
        fn = AsyncMock(side_effect=[TimeoutError("timed out"), "ok"])
        with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert fn.await_count == 2


class TestRetryClassification:
    @pytest.mark.parametrize(
        "error",
        [
            ProviderRequestError("HTTP 400: invalid schema"),
            ProviderError("unclassified provider failure"),
            TypeError("unexpected keyword argument"),
        ],
        ids=["request_error", "provider_error", "type_error"],
    )
    async def test_non_retryable_errors_fail_after_one_call(self, error):
        fn = AsyncMock(side_effect=error)
        with (
            patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock) as sleep,
            pytest.raises(type(error)),
        ):
            await retry_with_backoff(fn, max_retries=3)
        fn.assert_awaited_once()
        sleep.assert_not_awaited()

    @pytest.mark.parametrize(
        "error",
        [ProviderTransientError("HTTP 503"), ProviderTimeoutError("m", 30)],
        ids=["transient", "timeout"],
    )
    async def test_retryable_provider_errors_are_retried(self, error):
        fn = AsyncMock(side_effect=[error, "ok"])
        with patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock):
            assert await retry_with_backoff(fn, max_retries=3) == "ok"
        assert fn.await_count == 2

    def test_retryable_attribute_is_honoured_on_any_exception(self):
        error = RuntimeError("custom")
        error.retryable = False  # type: ignore[attr-defined]
        assert is_retryable(error) is False
        assert is_retryable(RuntimeError("plain")) is True

    @pytest.mark.parametrize(
        ("retry_after", "expected"),
        [(7.0, 7.0), (0.0, 1.0), (500.0, 60.0)],
        ids=["longer_than_backoff", "shorter_than_backoff", "capped_at_max_delay"],
    )
    async def test_retry_after_sets_the_minimum_wait(self, retry_after, expected):
        fn = AsyncMock(side_effect=[ProviderTransientError("429", retry_after=retry_after), "ok"])
        with (
            patch("delegatron.utils.retry.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("delegatron.utils.retry.random.uniform", return_value=0.0),
        ):
            await retry_with_backoff(fn, max_retries=3, base_delay=1.0, max_delay=60.0)
        sleep.assert_awaited_once_with(expected)
