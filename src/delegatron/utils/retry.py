"""Exponential backoff with jitter for API calls."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def is_retryable(error: Exception) -> bool:
    """Return whether another attempt at the call that raised *error* can succeed.

    ``ValueError`` (which covers JSONDecodeError and pydantic's ValidationError)
    and ``TypeError`` are deterministic client-side failures: bad model output
    is handled by the caller's own validation loop (BaseAgent.invoke), and a
    malformed request fails the same way every time. Provider errors carry a
    ``retryable`` flag (see ``agents.providers.base.ProviderError``); any other
    exception is assumed transient.
    """
    if isinstance(error, (ValueError, TypeError)):
        return False
    return bool(getattr(error, "retryable", True))


async def retry_with_backoff(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    **kwargs: Any,
) -> Any:
    """Retry an async function with exponential backoff and jitter.

    Only errors for which ``is_retryable`` holds are retried; others are
    re-raised immediately. An error's ``retry_after`` (seconds, parsed from
    the provider's ``retry-after`` headers) raises the wait to at least that
    long, still capped at *max_delay*.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            if not is_retryable(e):
                raise

            last_error = e
            if attempt == max_retries:
                break

            delay = min(base_delay * (2**attempt), max_delay)
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None:
                delay = max(delay, min(retry_after, max_delay))
            jitter = random.uniform(0, delay * 0.1)
            logger.warning(
                "Retryable error (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1,
                max_retries + 1,
                e,
                delay + jitter,
            )
            await asyncio.sleep(delay + jitter)

    raise last_error  # type: ignore[misc]
