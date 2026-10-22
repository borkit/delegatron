"""Abstract base class for LLM provider adapters."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

# ``ProviderResponse.finish_reason`` when the output was cut off at ``max_tokens``.
FINISH_REASON_LENGTH = "length"

# Read timeout for one model request when the role does not set ``timeout_seconds``
# (the OpenAI and Anthropic SDK default).  It bounds each read, so a streamed
# response may run longer as long as chunks keep arriving.
DEFAULT_TIMEOUT_SECONDS = 600

_CONNECT_TIMEOUT_SECONDS = 10.0

# HTTP statuses (besides 5xx) that a later attempt can get past.
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429})


@dataclass
class ProviderResponse:
    """Normalized response from any LLM provider.

    ``input_tokens`` is the whole prompt, cached tokens included;
    ``cache_read_tokens`` is the part served from the provider's prompt cache
    and ``cache_write_tokens`` the part written to it (billed at the cache rates).
    ``finish_reason`` is the provider's stop reason, normalized to
    ``FINISH_REASON_LENGTH`` when the output hit ``max_tokens``.
    """

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str = ""
    provider_request_id: str = ""
    raw_response: Any = None
    finish_reason: str = ""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = f"dlg-{uuid.uuid4().hex[:12]}"


class ProviderError(RuntimeError):
    """A model request failed.

    ``retryable`` tells ``utils.retry.retry_with_backoff`` whether another
    attempt can succeed; ``retry_after`` is the wait the provider asked for, in
    seconds.
    """

    retryable: bool = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    """The model did not answer within the role's ``timeout_seconds``."""

    retryable = True

    def __init__(self, model: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Model '{model}' did not respond within {timeout_seconds:g}s. "
            "Raise timeout_seconds for this role in delegatron.toml if it needs longer."
        )
        self.model = model
        self.timeout_seconds = timeout_seconds


class ProviderRequestError(ProviderError):
    """The provider rejected the request (bad request, auth, unknown model)."""


class ProviderTransientError(ProviderError):
    """Connection failure, throttling, server error, or a stream that broke off."""

    retryable = True


def token_count(usage: Any, name: str) -> int:
    """Read the token counter *name* from an SDK usage object; missing or ``None`` is 0."""
    value = getattr(usage, name, None) if usage is not None else None
    return value if isinstance(value, int) else 0


def http_timeout(timeout_seconds: float) -> httpx.Timeout:
    """HTTP timeout for a model request: ``timeout_seconds`` per read, 10 s to connect."""
    return httpx.Timeout(timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS)


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Return the wait requested by ``retry-after-ms`` / ``retry-after`` (seconds) headers."""
    if not headers:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return max(0.0, float(value) * scale)
        except (TypeError, ValueError):
            continue
    return None


def error_for_status(
    status: int, message: str, headers: Mapping[str, str] | None = None
) -> ProviderError:
    """Classify an HTTP error status: 408/409/429 and 5xx are transient, others are not."""
    if status in _TRANSIENT_STATUS_CODES or status >= 500:
        return ProviderTransientError(message, retry_after=retry_after_seconds(headers))
    return ProviderRequestError(message)


def stream_error(model: str, timeout_seconds: float, error: Exception) -> ProviderError:
    """Classify an error raised while reading a response that had already started."""
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ProviderTimeoutError(model, timeout_seconds)
    return ProviderTransientError(f"Response stream from '{model}' broke off: {error}")


class BaseProvider(ABC):
    """Base interface for LLM provider adapters.

    All providers normalize to the same request/response format:
    - Input: system prompt + user message (both strings)
    - Output: ProviderResponse with content, token counts, and raw response

    Providers handle their own authentication, request formatting,
    and structured output enforcement. They make a single attempt per call
    (SDK-internal retries are off; ``utils.retry`` is the only retry layer)
    and report failures as ``ProviderError`` subclasses.
    """

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        """Generate a response from the LLM.

        Args:
            system_prompt: System-level instructions for the model.
            user_message: The user's input/request.
            json_schema: Optional JSON schema to enforce structured output.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 = deterministic).

        Returns:
            Normalized ProviderResponse.
        """
        ...

    async def stream_generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> AsyncIterator[str | ProviderResponse]:
        """Stream tokens from the LLM, yielding each chunk as a string.

        The final item yielded is a ``ProviderResponse`` containing the full
        accumulated content and token usage metadata.  All preceding items are
        ``str`` token chunks.

        The default implementation falls back to the non-streaming ``generate``
        method and yields the entire response as a single chunk followed by the
        ``ProviderResponse``.

        Args:
            system_prompt: System-level instructions for the model.
            user_message: The user's input/request.
            json_schema: Optional JSON schema to enforce structured output.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 = deterministic).

        Yields:
            ``str`` token chunks followed by a final ``ProviderResponse``.
        """
        response = await self.generate(
            system_prompt=system_prompt,
            user_message=user_message,
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        yield response.content
        yield response
