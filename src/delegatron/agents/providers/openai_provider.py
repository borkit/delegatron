"""OpenAI provider adapter (also used for OpenAI-compatible endpoints)."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

try:
    import openai
    from openai import (
        APIConnectionError,
        APIError,
        APIStatusError,
        APITimeoutError,
        NotFoundError,
    )

    _HAS_OPENAI = True
except ImportError:
    openai = None  # type: ignore[assignment,unused-ignore]
    _HAS_OPENAI = False

    class _OpenAIUnavailableError(Exception):
        """Stand-in for the SDK's error classes so ``except`` clauses stay valid."""

    APIConnectionError = APIError = APIStatusError = _OpenAIUnavailableError  # type: ignore[assignment,misc,unused-ignore]
    APITimeoutError = NotFoundError = _OpenAIUnavailableError  # type: ignore[assignment,misc,unused-ignore]

from delegatron.agents.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    BaseProvider,
    ProviderError,
    ProviderRequestError,
    ProviderResponse,
    ProviderTimeoutError,
    ProviderTransientError,
    error_for_status,
    http_timeout,
    stream_error,
    token_count,
)
from delegatron.utils.provider import is_local_base_url

logger = logging.getLogger(__name__)

# Sent when a role has no key (local servers, or a key withheld by policy).
NO_API_KEY = "no-api-key"

# Context length Ollama runs a model with when neither the Modelfile nor
# OLLAMA_CONTEXT_LENGTH sets one.
OLLAMA_DEFAULT_CONTEXT_WINDOW = 4096

# Timeout for each request to Ollama's native API while sizing the context window.
_PROBE_TIMEOUT_SECONDS = 5.0

# Ollama errors that mean the model does not fit in memory; retrying only
# reloads it into the same memory.
_OLLAMA_OUT_OF_MEMORY = re.compile(
    r"out of memory|requires more system memory|insufficient memory|unable to allocate|\boom\b",
    re.IGNORECASE,
)


class OllamaConnectionError(ProviderError):
    """Ollama is unreachable, does not have the model, or failed running it."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _parse_num_ctx(parameters: str) -> int:
    """Extract ``num_ctx`` from the Modelfile parameters text of ``/api/show``."""
    for line in parameters.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "num_ctx":
            return int(parts[1])
    return 0


def _env_context_length() -> int:
    """Return a positive ``OLLAMA_CONTEXT_LENGTH`` from the environment, else 0."""
    try:
        return max(0, int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "")))
    except ValueError:
        return 0


def _cached_prompt_tokens(usage: Any) -> int:
    """Prompt tokens served from OpenAI's prompt cache (0 when the endpoint omits them)."""
    return token_count(getattr(usage, "prompt_tokens_details", None), "cached_tokens")


def _with_tag(model: str) -> str:
    """Ollama model name with its implicit ``:latest`` tag spelled out."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


def _loaded_context_length(ps: Any, model: str) -> int:
    """Runtime ``context_length`` of *model* in an ``/api/ps`` reply, 0 when not loaded."""
    wanted = _with_tag(model)
    for entry in ps.get("models") or []:
        names = {entry.get("name") or "", entry.get("model") or ""}
        if wanted in {_with_tag(name) for name in names if name}:
            length = entry.get("context_length")
            return length if isinstance(length, int) and length > 0 else 0
    return 0


class OpenAIProvider(BaseProvider):
    """Provider adapter for OpenAI API and any OpenAI-compatible endpoint.

    Supports structured output via json_schema response format.
    Works with: OpenAI, Ollama, Grok (xAI), GitHub Copilot, Together,
    Groq, Fireworks, LMStudio, and any other OpenAI-compatible API.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not _HAS_OPENAI:
            raise RuntimeError("OpenAI SDK not installed. Install with: pip install openai")
        kwargs: dict[str, Any] = {
            # Never empty: newer SDKs read OPENAI_API_KEY from the environment
            # when the key is missing, which would bypass the env-key policy.
            "api_key": api_key or NO_API_KEY,
            "max_retries": 0,
            "timeout": http_timeout(timeout_seconds),
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = model
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._is_ollama = bool(base_url and ("11434" in base_url or "ollama" in base_url.lower()))
        self._context_window: int | None = None
        # Trained context length (caps the window) and whether the window was
        # not pinned by the Modelfile, so /api/ps is re-read once the model runs.
        self._trained_context = 0
        self._recheck_window = False

    async def detect_context_window(self) -> int:
        """Return the context window (in tokens) the model is served with; 0 = unknown.

        Only a server that answers Ollama's ``GET /api/version`` is sized, so
        LM Studio, vLLM, llama.cpp, a LiteLLM proxy or a cloud API is unknown
        (0) and its prompts are not trimmed unless the role sets
        ``context_window``. Endpoints that cannot be Ollama (no Ollama marker,
        not a local or private host) are not asked at all.

        For Ollama (on this machine or on the network) the window is the
        Modelfile ``num_ctx``, else the runtime ``context_length`` of the
        loaded model (``/api/ps``), else ``OLLAMA_CONTEXT_LENGTH`` from the
        environment, else Ollama's 4096 default, capped by the model's trained
        context length. Unless the Modelfile pins it, the window is re-read
        from ``/api/ps`` once after the first successful request, when the
        model is loaded. Cached, except that a server that is unreachable or
        answers with a transient error is asked again on the next call; never
        raises.
        """
        if self._context_window is None:
            if not self._may_be_ollama():
                self._context_window = 0
            else:
                window = await self._query_context_window()
                if window is None:
                    return 0
                self._context_window = window
        return self._context_window

    def _may_be_ollama(self) -> bool:
        """Whether the endpoint can be an Ollama server worth asking for its window."""
        return self._is_ollama or is_local_base_url(self._base_url)

    def _native_api(self) -> tuple[Any, str]:
        """SDK client for Ollama's native API (no retries, short timeout) and the API root."""
        # Reuse the SDK client (auth headers, transport) for the native API.
        client = self._client.with_options(max_retries=0, timeout=_PROBE_TIMEOUT_SECONDS)
        return client, self._base_url.rstrip("/").removesuffix("/v1")

    async def _query_context_window(self) -> int | None:
        """This model's window if the endpoint is Ollama, 0 if it is not, None if unreachable."""
        client, root = self._native_api()
        is_ollama = await self._is_ollama_server(client, root)
        if is_ollama is None:
            return None
        if not is_ollama:
            logger.warning(
                "%s at %s is not an Ollama server, so its context window is unknown and "
                "prompts are not trimmed; set context_window for this role to enable trimming",
                self._model,
                self._base_url,
            )
            return 0

        num_ctx = 0
        try:
            response = await client.post(
                f"{root}/api/show", cast_to=httpx.Response, body={"model": self._model}
            )
            info = response.json()
            num_ctx = _parse_num_ctx(info.get("parameters") or "")
            self._trained_context = max(
                (
                    int(value)
                    for key, value in (info.get("model_info") or {}).items()
                    if key.endswith(".context_length")
                ),
                default=0,
            )
        except Exception as e:
            logger.debug("Could not read %s model info from %s: %s", self._model, root, e)

        self._recheck_window = not num_ctx
        window = self._capped(
            num_ctx
            or await self._runtime_context_length(client, root)
            or _env_context_length()
            or OLLAMA_DEFAULT_CONTEXT_WINDOW
        )
        logger.info("Context window for %s: %d tokens", self._model, window)
        return window

    async def _is_ollama_server(self, client: Any, root: str) -> bool | None:
        """Whether ``GET /api/version`` answers the way Ollama does.

        None when the server cannot answer yet (unreachable, or a transient
        status such as a proxy's 502), so it is asked again on the next call.
        """
        try:
            response = await client.get(f"{root}/api/version", cast_to=httpx.Response)
            reply = response.json()
        except (APIConnectionError, httpx.TransportError) as e:
            logger.debug("Could not reach %s to size the context window: %s", root, e)
            return None
        except APIStatusError as e:
            if error_for_status(e.status_code, "").retryable:
                logger.debug("%s is not ready to size the context window: %s", root, e)
                return None
            logger.debug("No Ollama version endpoint at %s: %s", root, e)
            return False
        except Exception as e:
            logger.debug("No Ollama version endpoint at %s: %s", root, e)
            return False
        return isinstance(reply, dict) and isinstance(reply.get("version"), str)

    async def _runtime_context_length(self, client: Any, root: str) -> int:
        """Context length the loaded model runs with (``/api/ps``), 0 when not loaded."""
        try:
            response = await client.get(f"{root}/api/ps", cast_to=httpx.Response)
            return _loaded_context_length(response.json(), self._model)
        except Exception as e:
            logger.debug("Could not list the models loaded at %s: %s", root, e)
            return 0

    def _capped(self, window: int) -> int:
        """*window* capped by the model's trained context length."""
        return min(window, self._trained_context) if self._trained_context else window

    async def _recheck_context_window(self) -> None:
        """Once, after a successful request, take the window the loaded model runs with."""
        if not self._recheck_window:
            return
        self._recheck_window = False
        loaded = await self._runtime_context_length(*self._native_api())
        if loaded and self._capped(loaded) != self._context_window:
            self._context_window = self._capped(loaded)
            logger.info(
                "Context window for %s (loaded model): %d tokens", self._model, self._context_window
            )

    def _build_request_kwargs(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Build the kwargs dict for ``chat.completions.create``."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Use structured output if schema provided
        if json_schema:
            schema_name = json_schema.get("title", "response").lower().replace(" ", "_")
            # Try json_schema format (OpenAI native)
            # Fall back to json_object for compatible endpoints that don't support full schema
            if not self._base_url or "openai.com" in self._base_url:
                # Non-strict: the Pydantic schemas (optional fields, dict-valued
                # PatchSet.files) do not meet strict mode's rules; the agent
                # validates the output against the model anyway.
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": json_schema,
                        "strict": False,
                    },
                }
            else:
                # OpenAI-compatible endpoints: use json_object mode + schema in prompt
                kwargs["response_format"] = {"type": "json_object"}
                messages[0]["content"] += (
                    f"\n\nYou MUST respond with valid JSON matching this schema:\n"
                    f"```json\n{json.dumps(json_schema, indent=2)}\n```"
                )

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
            response = await self._client.chat.completions.create(**kwargs)
        except (APIConnectionError, APIStatusError) as e:
            raise self._request_error(e) from e

        if not response.choices:
            raise RuntimeError(f"OpenAI returned empty choices for model {self._model}")
        choice = response.choices[0]
        content = choice.message.content or ""
        await self._recheck_context_window()

        usage = response.usage
        return ProviderResponse(
            content=content,
            model=response.model or self._model,
            input_tokens=token_count(usage, "prompt_tokens"),
            output_tokens=token_count(usage, "completion_tokens"),
            provider_request_id=getattr(response, "id", "") or "",
            raw_response=response,
            finish_reason=choice.finish_reason or "",
            cache_read_tokens=_cached_prompt_tokens(usage),
        )

    async def stream_generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> AsyncIterator[str | ProviderResponse]:
        """Stream tokens from the OpenAI-compatible API.

        Yields ``str`` token chunks as they arrive, then a final
        ``ProviderResponse`` with the full content and usage metadata.
        """
        kwargs = self._build_request_kwargs(
            system_prompt,
            user_message,
            json_schema,
            max_tokens,
            temperature,
        )
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except (APIConnectionError, APIStatusError) as e:
            raise self._request_error(e) from e

        chunks: list[str] = []
        usage: Any = None
        response_model = self._model
        response_id = ""
        finish_reason = ""

        try:
            async for event in stream:
                if not response_id and getattr(event, "id", None):
                    response_id = event.id
                if getattr(event, "model", None):
                    response_model = event.model

                # Token usage arrives on the final chunk (stream_options.include_usage)
                if getattr(event, "usage", None) is not None:
                    usage = event.usage

                if event.choices:
                    choice = event.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                    token = getattr(choice.delta, "content", None) or ""
                    if token:
                        chunks.append(token)
                        yield token
        except APITimeoutError as e:
            raise ProviderTimeoutError(self._model, self._timeout_seconds) from e
        except (APIError, httpx.TransportError) as e:
            raise stream_error(self._model, self._timeout_seconds, e) from e

        await self._recheck_context_window()
        yield ProviderResponse(
            content="".join(chunks),
            model=response_model,
            input_tokens=token_count(usage, "prompt_tokens"),
            output_tokens=token_count(usage, "completion_tokens"),
            provider_request_id=response_id,
            finish_reason=finish_reason,
            cache_read_tokens=_cached_prompt_tokens(usage),
        )

    def _request_error(self, e: Exception) -> ProviderError:
        """Map an SDK error raised before any response arrived to a ``ProviderError``."""
        if isinstance(e, APITimeoutError):
            return ProviderTimeoutError(self._model, self._timeout_seconds)
        if isinstance(e, NotFoundError):
            return self._friendly_not_found_error(e)
        if isinstance(e, APIStatusError):
            return self._friendly_status_error(e)
        return self._friendly_connection_error(e)

    def _friendly_connection_error(self, e: Exception) -> ProviderError:
        """Produce a clear error message when the provider is unreachable."""
        if self._is_ollama:
            return OllamaConnectionError(
                f"Cannot connect to Ollama at {self._base_url}.\n"
                f"  - Is Ollama running? Start it with: ollama serve\n"
                f"  - Check the URL is correct (default: http://localhost:11434/v1)\n"
                f"  Original error: {e}",
                retryable=True,
            )
        return ProviderTransientError(
            f"Cannot connect to API at {self._base_url or 'api.openai.com'}. "
            f"Check your network and base_url setting. Error: {e}"
        )

    def _friendly_not_found_error(self, e: Exception) -> ProviderError:
        """Produce a clear error when a model is not available."""
        if self._is_ollama:
            return OllamaConnectionError(
                f"Model '{self._model}' not found in Ollama.\n"
                f"  - Pull it first: ollama pull {self._model}\n"
                f"  - List available models: ollama list\n"
                f"  - Check for typos in the model name\n"
                f"  Original error: {e}",
                retryable=False,
            )
        return ProviderRequestError(
            f"Model '{self._model}' not found. Check the model name "
            f"is correct for your provider. Error: {e}"
        )

    def _friendly_status_error(self, e: APIStatusError) -> ProviderError:
        """Produce clear errors for common API failures."""
        status = e.status_code
        body = str(e.body) if e.body else str(e)

        if self._is_ollama:
            # Retrying an out-of-memory model only reloads it into the same memory.
            if status == 500 and _OLLAMA_OUT_OF_MEMORY.search(body):
                return OllamaConnectionError(
                    f"Ollama ran out of memory running '{self._model}'.\n"
                    f"  - Try a smaller model: local preset or check Ollama is running\n"
                    f"  - Try a smaller quantization: {self._model.replace(':14b', ':7b')}\n"
                    f"  - Close other applications to free RAM/VRAM\n"
                    f"  - Reduce max_tokens in your config\n"
                    f"  Error: {body[:300]}",
                    retryable=False,
                )
            if status == 500:
                model = self._model
                return OllamaConnectionError(
                    f"Ollama error (HTTP 500) with model '{model}'.\n"
                    f"  - The model may be corrupted. Try: "
                    f"ollama rm {model} && ollama pull {model}\n"
                    f"  - Check Ollama logs: journalctl -u ollama\n"
                    f"  Error: {body[:300]}",
                    retryable=True,
                )

        if status == 401:
            message = (
                f"Authentication failed (HTTP 401). Check your API key. "
                f"Provider: {self._base_url or 'OpenAI'}"
            )
        elif status == 429:
            message = (
                "Rate limited (HTTP 429). The provider is throttling requests. "
                "Wait a moment and try again, or reduce request frequency."
            )
        else:
            message = f"API error (HTTP {status}): {body[:300]}"
        return error_for_status(status, message, e.response.headers)
