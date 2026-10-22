"""Anthropic Claude provider adapter.

Requires the ``anthropic`` SDK: ``pip install anthropic``
or install via the ``[cloud-providers]`` optional dependency group.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

try:
    import anthropic

    _HAS_ANTHROPIC = True
except ImportError:
    anthropic = None  # type: ignore[assignment,unused-ignore]
    _HAS_ANTHROPIC = False

from delegatron.agents.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    FINISH_REASON_LENGTH,
    BaseProvider,
    ProviderError,
    ProviderResponse,
    ProviderTimeoutError,
    ProviderTransientError,
    error_for_status,
    http_timeout,
    stream_error,
    token_count,
)

# Anthropic accepts temperatures in [0, 1]; roles may set up to 2.0 (the OpenAI/Gemini range).
_MAX_TEMPERATURE = 1.0

# Models that still accept a non-default ``temperature``: Claude 3.x and the
# 4.x family up to 4.6. Opus 4.7+ and every 5.x model return HTTP 400 for it,
# so unknown (newer) models get no temperature at all.
_SAMPLING_MODELS = re.compile(r"claude-(3|(opus|sonnet|haiku)-4(?!-[78](?!\d)))")

# Models that accept a forced ``tool_choice`` (``tool``/``any``). Fable 5.1,
# Mythos 5.1, Opus 5.5 and Sonnet 5.5 return HTTP 400 for it, as newer models
# are assumed to; those get ``auto`` instead. (``strict: true`` is not set:
# the agent schemas don't meet its constraints.)
_FORCED_TOOL_MODELS = re.compile(
    r"claude-(3|(opus|sonnet|haiku)-4|(opus|sonnet|fable|mythos)-5(?!-\d(?!\d))|haiku-5)"
)


def _model_name(model: str) -> str:
    """*model* without a platform prefix (``anthropic.`` on Bedrock)."""
    return model.rsplit(".", 1)[-1] if model.startswith("anthropic.") else model


def _accepts_temperature(model: str) -> bool:
    return _SAMPLING_MODELS.match(_model_name(model)) is not None


def _accepts_forced_tool(model: str) -> bool:
    return _FORCED_TOOL_MODELS.match(_model_name(model)) is not None


def _finish_reason(stop_reason: str | None) -> str:
    """Normalize Anthropic's ``stop_reason`` (``max_tokens`` means truncated)."""
    return FINISH_REASON_LENGTH if stop_reason == "max_tokens" else stop_reason or ""


def _prompt_tokens(usage: Any) -> tuple[int, int, int]:
    """``(input, cache_read, cache_write)`` tokens; ``input`` includes both cache counts.

    Anthropic reports ``input_tokens`` without the tokens read from or written
    to the prompt cache.
    """
    cache_read = token_count(usage, "cache_read_input_tokens")
    cache_write = token_count(usage, "cache_creation_input_tokens")
    return token_count(usage, "input_tokens") + cache_read + cache_write, cache_read, cache_write


class AnthropicProvider(BaseProvider):
    """Provider adapter for Anthropic's Claude API.

    Uses tool_use with a single tool to enforce structured JSON output
    when a json_schema is provided.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        env_auth: bool = True,
    ) -> None:
        if not _HAS_ANTHROPIC:
            raise RuntimeError("Anthropic SDK not installed. Install with: pip install anthropic")
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": http_timeout(timeout_seconds),
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)
        if not env_auth:
            # The SDK reads ANTHROPIC_AUTH_TOKEN itself and sends it to any
            # base_url; a role barred from environment keys must not send it.
            self._client.auth_token = None
        self._model = model
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _build_request_kwargs(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Build the kwargs dict for ``messages.create``.

        *temperature* is clamped to Anthropic's ``[0, 1]`` range, and left out
        for models that reject a non-default temperature.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        if _accepts_temperature(self._model):
            kwargs["temperature"] = min(max(temperature, 0.0), _MAX_TEMPERATURE)

        # Use tool_use to enforce structured output
        if json_schema:
            tool_name = json_schema.get("title", "structured_output").lower().replace(" ", "_")
            tool: dict[str, Any] = {
                "name": tool_name,
                "description": f"Return structured {tool_name} output.",
                "input_schema": json_schema,
            }
            if _accepts_forced_tool(self._model):
                kwargs["tool_choice"] = {"type": "tool", "name": tool_name}
            else:
                # The call is not guaranteed; a text answer is still parsed as
                # JSON, and one that fails validation is retried by the agent.
                tool["description"] = f"Always call this tool to return your {tool_name} output."
                kwargs["tool_choice"] = {"type": "auto"}
            kwargs["tools"] = [tool]

        return kwargs

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        kwargs = self._build_request_kwargs(
            system_prompt,
            user_message,
            json_schema,
            max_tokens,
            temperature,
        )

        try:
            response = await self._client.messages.create(**kwargs)
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            raise self._request_error(e) from e

        # Extract content — prefer tool_use (structured) over text
        content = ""
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                # Structured output takes priority — return immediately
                content = json.dumps(block.input)
                break
            elif block.type == "text":
                text_parts.append(block.text)
        if not content:
            content = "\n".join(text_parts)

        input_tokens, cache_read, cache_write = _prompt_tokens(response.usage)
        return ProviderResponse(
            content=content,
            model=response.model,
            input_tokens=input_tokens,
            output_tokens=token_count(response.usage, "output_tokens"),
            provider_request_id=getattr(response, "id", "") or "",
            raw_response=response,
            finish_reason=_finish_reason(response.stop_reason),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    async def stream_generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> AsyncIterator[str | ProviderResponse]:
        """Stream tokens from the Anthropic API.

        Yields ``str`` token chunks as they arrive, then a final
        ``ProviderResponse`` with the full content and usage metadata.

        Note: When ``json_schema`` is provided, structured output uses
        tool_use which the Anthropic streaming API surfaces via
        ``input_json`` delta events.
        """
        kwargs = self._build_request_kwargs(
            system_prompt,
            user_message,
            json_schema,
            max_tokens,
            temperature,
        )

        text_chunks: list[str] = []
        tool_chunks: list[str] = []
        input_tokens = cache_read = cache_write = 0
        output_tokens = 0
        response_id = ""
        stop_reason: str | None = None
        connected = False

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                connected = True
                async for event in stream:
                    # anthropic SDK events: message_start, content_block_delta, message_delta, etc.
                    event_type = getattr(event, "type", "")

                    if event_type == "message_start":
                        msg = getattr(event, "message", None)
                        if msg:
                            response_id = getattr(msg, "id", "") or ""
                            usage = getattr(msg, "usage", None)
                            if usage:
                                input_tokens, cache_read, cache_write = _prompt_tokens(usage)

                    elif event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta:
                            delta_type = getattr(delta, "type", "")
                            if delta_type == "text_delta":
                                token = getattr(delta, "text", "") or ""
                                if token:
                                    text_chunks.append(token)
                                    yield token
                            elif delta_type == "input_json_delta":
                                # Accumulate tool_use JSON separately from text,
                                # matching non-streaming path which prefers tool_use
                                token = getattr(delta, "partial_json", "") or ""
                                if token:
                                    tool_chunks.append(token)
                                    yield token

                    elif event_type == "message_delta":
                        delta = getattr(event, "delta", None)
                        stop_reason = getattr(delta, "stop_reason", None) or stop_reason
                        usage = getattr(event, "usage", None)
                        if usage:
                            output_tokens = token_count(usage, "output_tokens")
        except (anthropic.APIError, httpx.TransportError) as e:
            if connected:
                raise stream_error(self._model, self._timeout_seconds, e) from e
            raise self._request_error(e) from e

        # Prefer tool_use JSON (structured output) over text, matching
        # the non-streaming generate() path which picks tool_use first
        full_content = "".join(tool_chunks) if tool_chunks else "".join(text_chunks)
        yield ProviderResponse(
            content=full_content,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=response_id,
            finish_reason=_finish_reason(stop_reason),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def _request_error(self, e: Exception) -> ProviderError:
        """Map an SDK error raised before any response arrived to a ``ProviderError``."""
        if isinstance(e, anthropic.APITimeoutError):
            return ProviderTimeoutError(self._model, self._timeout_seconds)
        if isinstance(e, anthropic.APIStatusError):
            return error_for_status(
                e.status_code,
                f"Anthropic API error (HTTP {e.status_code}) for model '{self._model}': {e}",
                e.response.headers,
            )
        return ProviderTransientError(
            f"Cannot connect to the Anthropic API at "
            f"{self._base_url or 'api.anthropic.com'}. Error: {e}"
        )
