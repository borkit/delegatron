"""Google Gemini provider adapter.

Requires the ``google-genai`` SDK: ``pip install google-genai``
or install via the ``[cloud-providers]`` optional dependency group.
"""

from __future__ import annotations

from typing import Any

import httpx

try:
    from google import genai
    from google.genai import errors, types

    _HAS_GOOGLE = True
except ImportError:
    genai = None  # type: ignore[assignment,unused-ignore]
    errors = None  # type: ignore[assignment,unused-ignore]
    types = None  # type: ignore[assignment,unused-ignore]
    _HAS_GOOGLE = False

from delegatron.agents.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    FINISH_REASON_LENGTH,
    BaseProvider,
    ProviderResponse,
    ProviderTimeoutError,
    ProviderTransientError,
    error_for_status,
    token_count,
)


class GoogleProvider(BaseProvider):
    """Provider adapter for Google's Gemini API.

    Uses controlled generation with a JSON Schema (``response_json_schema``)
    for structured output.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not _HAS_GOOGLE:
            raise RuntimeError(
                "Google GenAI SDK not installed. Install with: pip install google-genai"
            )
        # The SDK does not retry unless retry_options are set; its timeout is in ms.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self._model = model
        self._timeout_seconds = timeout_seconds

    def _build_config(
        self,
        system_prompt: str,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> types.GenerateContentConfig:
        """Build the ``GenerateContentConfig`` for ``generate_content``.

        The schema is sent as plain JSON Schema: ``response_schema`` only takes
        the OpenAPI subset, which rejects ``additionalProperties`` (PatchSet.files).
        """
        return types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_schema else None,
            response_json_schema=json_schema or None,
        )

    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse:
        config = self._build_config(system_prompt, json_schema, max_tokens, temperature)

        # Client-side errors (ValueError/TypeError from request building) propagate
        # unchanged: they are deterministic, so they must not be retried.
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=user_message,
                config=config,
            )
        except errors.APIError as e:
            raise error_for_status(
                e.code,
                f"Google GenAI API error for model {self._model}: {e}",
                getattr(e.response, "headers", None),
            ) from e
        except (TimeoutError, httpx.TimeoutException) as e:
            raise ProviderTimeoutError(self._model, self._timeout_seconds) from e
        except (OSError, httpx.TransportError) as e:
            raise ProviderTransientError(
                f"Cannot connect to the Gemini API for model {self._model}. Error: {e}"
            ) from e

        try:
            content = response.text or ""
        except ValueError:
            # Safety filters or empty response — .text raises ValueError
            content = ""

        # prompt_token_count already includes the tokens served from the
        # (implicit) context cache, reported in cached_content_token_count.
        # Thinking tokens are billed as output, as the other providers count them.
        usage = response.usage_metadata
        return ProviderResponse(
            content=content,
            model=self._model,
            input_tokens=token_count(usage, "prompt_token_count"),
            output_tokens=token_count(usage, "candidates_token_count")
            + token_count(usage, "thoughts_token_count"),
            raw_response=response,
            finish_reason=_finish_reason(response),
            cache_read_tokens=token_count(usage, "cached_content_token_count"),
        )


def _finish_reason(response: types.GenerateContentResponse) -> str:
    """Normalize the first candidate's ``finish_reason`` (``MAX_TOKENS`` means truncated)."""
    reason = response.candidates[0].finish_reason if response.candidates else None
    if reason == types.FinishReason.MAX_TOKENS:
        return FINISH_REASON_LENGTH
    return reason.value if reason else ""
