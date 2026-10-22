"""Abstract base class for all agents."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, TypeVar, cast

from pydantic import BaseModel, ValidationError

from delegatron.agents.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    FINISH_REASON_LENGTH,
    BaseProvider,
    ProviderResponse,
)
from delegatron.config import clamp_temperature
from delegatron.prompts import load_prompt
from delegatron.utils.context_budget import (
    DEFAULT_TRIM_KEYS,
    estimate_tokens,
    fit_input_to_budget,
)
from delegatron.utils.retry import is_retryable, retry_with_backoff
from delegatron.utils.test_output import failed_test_ids

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Max retries when the model returns invalid JSON/schema
MAX_VALIDATION_RETRIES = 2

# Context tokens kept free for the response: a quarter of the window, at least this many
MIN_OUTPUT_RESERVE_TOKENS = 1024

# Context tokens kept free for the note a retry appends to the user message.
RETRY_RESERVE_TOKENS = 256

# Characters of a validation error quoted in the retry note (fits RETRY_RESERVE_TOKENS).
MAX_RETRY_ERROR_CHARS = 600

# A server that reports this close to the whole window as prompt tokens most
# likely cut the prompt to fit (Ollama truncates silently at num_ctx).
SATURATION_MARGIN_TOKENS = 16

# A response cut off at max_tokens is retried once with this budget multiplier,
# up to the cap; the retry never gets less than the first call had.
TRUNCATION_RETRY_FACTOR = 2
MAX_TRUNCATION_RETRY_TOKENS = 32768

# Prompts (prompts/<name>/v1.md) appended to the user message of a retry.
TRUNCATION_RETRY_PROMPT = "truncation_retry"
VALIDATION_RETRY_PROMPT = "validation_retry"

# Agent-side deadline for one provider call (or the gap between two streamed
# chunks), as a multiple of the provider's own request timeout.  A backstop for
# a client that never times out by itself.
CALL_DEADLINE_FACTOR = 1.5

# Chain-of-thought blocks (DeepSeek-R1, Qwen3) that precede the JSON answer.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

# Input keys whose items may name a file the attempt must change (``fix_file``/``file``).
_FEEDBACK_KEYS = ("review_feedback", "director_guidance", "previous_issues", "conflicts")


class AgentOutputError(ValueError):
    """The model gave no usable output; ``meta`` holds the usage of every attempt."""

    def __init__(self, message: str, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.meta: dict[str, Any] = meta or {}


class TruncatedOutputError(AgentOutputError):
    """The model's output hit the token limit and could not be retried to completion."""

    def __init__(
        self,
        model: str,
        output_tokens: int,
        max_tokens: int,
        meta: dict[str, Any] | None = None,
    ) -> None:
        advice = (
            "Raise this role's max_tokens (and the model's context window) or split the task."
            if max_tokens < MAX_TRUNCATION_RETRY_TOKENS
            else "Split the task into smaller ones."
        )
        super().__init__(
            f"Output from {model} was cut off at the token limit "
            f"({output_tokens} output tokens, max_tokens={max_tokens}). {advice}",
            meta,
        )
        self.model = model
        self.output_tokens = output_tokens
        self.max_tokens = max_tokens


class BaseAgent(ABC):
    """Base interface for Delegatron agents.

    Each agent receives structured input and produces a schema-validated artifact.
    Agents are stateless — the orchestrator manages all state between calls.

    ``input_data["_temperature"]`` overrides the sampling temperature for one
    call (clamped to [0, 2] like the configured one), and
    ``input_data["_protected_files"]`` names files the context guard must not
    drop (besides the task outputs, failing-test files and files named by
    feedback, which it finds itself).  When the context window is known
    (configured, or detected from the provider on first use), the input keys
    in ``_context_trim_keys`` are shrunk so the rendered prompt fits; what was
    trimmed is reported in ``_meta["context_trimmed"]``.  ``_meta`` token
    counts cover every attempt the call made, listed in ``_meta["attempts"]``.
    """

    # Input keys the context-window guard may shrink, least important first.
    _context_trim_keys: ClassVar[tuple[str, ...]] = DEFAULT_TRIM_KEYS
    # ``{trimmed key: listing key}``: files the guard drops from the first are
    # listed by path and size under the second, so the model knows they exist.
    _dropped_files_listing: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        provider: BaseProvider,
        system_prompt: str,
        max_tokens: int = 16384,
        temperature: float = 0.0,
        context_window: int = 0,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = clamp_temperature(temperature)
        # 0 = ask the provider on each call (it caches, and re-reads a window
        # that changes once the model is loaded or a server comes back)
        self._configured_window = context_window
        self._context_window = context_window  # the window last used
        self._call_deadline: float | None = (
            timeout_seconds * CALL_DEADLINE_FACTOR if timeout_seconds > 0 else None
        )  # None: no deadline

    @abstractmethod
    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        """Build the user message from input data. Subclasses implement this."""
        ...

    @abstractmethod
    def _get_output_schema(self) -> dict[str, Any]:
        """Return the JSON schema for the expected output."""
        ...

    @abstractmethod
    def _get_output_model(self) -> type[BaseModel]:
        """Return the Pydantic model class for output validation."""
        ...

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Run the agent and return a schema-validated artifact.

        Attempts to generate structured output, validates against the Pydantic model,
        and retries with validation errors if the output is malformed.  Output cut
        off at the token limit is retried once with a larger budget; a second cut
        (or a cut on the last attempt) raises :class:`TruncatedOutputError`.
        """
        json_schema = self._get_output_schema()
        user_message, temperature, trim = await self._prepare_call(input_data, json_schema)
        return await self._complete(user_message, temperature, trim, json_schema)

    async def stream_invoke(
        self, input_data: dict[str, Any]
    ) -> AsyncIterator[str | dict[str, Any]]:
        """Run the agent with streaming, yielding tokens then the final artifact.

        Yields ``str`` token chunks as they arrive from the provider.  The very
        last item yielded is the validated ``dict`` artifact (same shape as the
        return value of :meth:`invoke`).

        A streamed response that is truncated or fails validation continues on
        the non-streaming retry path as its first attempt, so the retry carries
        the error instead of replaying the request.  A stream that breaks off
        with a retryable provider error is retried without streaming.
        """
        json_schema = self._get_output_schema()
        user_message, temperature, trim = await self._prepare_call(input_data, json_schema)

        stream = self._provider.stream_generate(
            system_prompt=self._system_prompt,
            user_message=user_message,
            json_schema=json_schema,
            max_tokens=self._max_tokens,
            temperature=temperature,
        )

        response: ProviderResponse | None = None
        try:
            async for item in self._within_deadline(stream):
                if isinstance(item, ProviderResponse):
                    response = item
                else:
                    yield item  # str token chunk
        except Exception as e:
            if not is_retryable(e):
                raise
            logger.warning(
                "%s: response stream failed (%s); retrying without streaming",
                type(self).__name__,
                e,
            )
            yield await self._complete(user_message, temperature, trim, json_schema)
            return

        if response is None:
            raise ValueError("Provider stream ended without a ProviderResponse")
        yield await self._complete(user_message, temperature, trim, json_schema, first=response)

    async def context_window(self) -> int:
        """The context window in tokens (0 = unknown): the configured one, else detected."""
        return await self._resolve_context_window()

    def prompt_overhead_tokens(self) -> int:
        """Tokens every call spends on the system prompt and the output schema."""
        return estimate_tokens(self._system_prompt) + estimate_tokens(
            json.dumps(self._get_output_schema(), indent=2)
        )

    async def input_budget_tokens(self) -> int:
        """Tokens the user message may take in this agent's context window.

        ``0`` when the window is unknown (no limit).  The window is detected
        from the provider on first use, like a call does.
        """
        window = await self._resolve_context_window()
        if window <= 0:
            return 0
        return max(self._input_budget(window, self._get_output_schema()), 1)

    def _input_budget(self, window: int, json_schema: dict[str, Any]) -> int:
        """*window* minus the system prompt, the schema, the output and the retry reserve."""
        # The schema travels with the system prompt (or as response_format).
        reserved = estimate_tokens(self._system_prompt) + estimate_tokens(
            json.dumps(json_schema, indent=2)
        )
        output_reserve = max(MIN_OUTPUT_RESERVE_TOKENS, window // 4)
        return window - reserved - output_reserve - RETRY_RESERVE_TOKENS

    async def _complete(
        self,
        user_message: str,
        temperature: float,
        trim: dict[str, Any] | None,
        json_schema: dict[str, Any],
        first: ProviderResponse | None = None,
    ) -> dict[str, Any]:
        """Generate until a response validates; *first* is an already received first attempt."""
        output_model = self._get_output_model()
        max_tokens = self._max_tokens
        attempts: list[ProviderResponse] = []
        cut_off: ProviderResponse | None = None  # the truncated response being retried
        retry_note = ""
        last_error = ""
        for attempt in range(1 + MAX_VALIDATION_RETRIES):
            if attempt == 0 and first is not None:
                response = first
            else:
                try:
                    response = await self._generate(
                        self._retry_message(user_message, retry_note),
                        json_schema,
                        max_tokens,
                        temperature,
                    )
                except Exception as e:
                    if (
                        cut_off is not None
                        and max_tokens > self._max_tokens
                        and not is_retryable(e)
                    ):
                        # The larger budget was refused (above the model's output cap).
                        logger.warning(
                            "%s: retry with max_tokens=%d was rejected: %s",
                            type(self).__name__,
                            max_tokens,
                            e,
                        )
                        raise TruncatedOutputError(
                            cut_off.model,
                            cut_off.output_tokens,
                            self._max_tokens,
                            self._usage_meta(attempts, temperature, trim),
                        ) from e
                    raise
            attempts.append(response)
            self._warn_if_saturated(response)
            last_attempt = attempt == MAX_VALIDATION_RETRIES

            if response.finish_reason == FINISH_REASON_LENGTH:
                # Never parse a cut-off response: a fragment of it may still validate.
                if cut_off is not None or last_attempt:
                    raise TruncatedOutputError(
                        response.model,
                        response.output_tokens,
                        max_tokens,
                        self._usage_meta(attempts, temperature, trim),
                    )
                cut_off = response
                last_error = f"The response was cut off at the {max_tokens}-token output limit."
                retry_note = self._truncation_note(response.output_tokens, max_tokens)
                max_tokens = max(
                    max_tokens,
                    min(max_tokens * TRUNCATION_RETRY_FACTOR, MAX_TRUNCATION_RETRY_TOKENS),
                )
                logger.warning(
                    "%s: output from %s truncated after %d tokens; retrying with max_tokens=%d",
                    type(self).__name__,
                    response.model,
                    response.output_tokens,
                    max_tokens,
                )
                continue

            try:
                parsed = self._parse_response(response, output_model)
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                retry_note = self._validation_note(last_error)
                logger.warning(
                    "Agent output validation failed (attempt %d/%d): %s",
                    attempt + 1,
                    1 + MAX_VALIDATION_RETRIES,
                    last_error,
                )
                continue
            parsed["_meta"] = self._usage_meta(attempts, temperature, trim)
            return parsed

        raise AgentOutputError(
            f"Agent failed to produce valid output after {1 + MAX_VALIDATION_RETRIES} attempts. "
            f"Last error: {last_error}",
            self._usage_meta(attempts, temperature, trim),
        )

    @staticmethod
    def _retry_message(user_message: str, retry_note: str) -> str:
        """The user message, with the note about the previous attempt appended on a retry."""
        if not retry_note:
            return user_message
        return f"{user_message}\n\n{retry_note}"

    @staticmethod
    def _validation_note(error: str) -> str:
        """The retry note for output that failed to parse or validate."""
        if len(error) > MAX_RETRY_ERROR_CHARS:
            error = error[:MAX_RETRY_ERROR_CHARS] + " ..."
        return _retry_prompt(VALIDATION_RETRY_PROMPT, {"error": error}) or (
            f"Error in your previous response:\n{error}"
        )

    @staticmethod
    def _truncation_note(output_tokens: int, max_tokens: int) -> str:
        """The retry note for output cut off at *max_tokens*."""
        values = {"output_tokens": str(output_tokens), "max_tokens": str(max_tokens)}
        return _retry_prompt(TRUNCATION_RETRY_PROMPT, values) or (
            f"Your previous response was cut off at the {max_tokens}-token output limit."
        )

    def _warn_if_saturated(self, response: ProviderResponse) -> None:
        """Warn when the prompt filled the context window (the server likely cut it)."""
        window = self._context_window
        if window > 0 and response.input_tokens >= window - SATURATION_MARGIN_TOKENS:
            logger.warning(
                "%s: the prompt used %d of %d context tokens; the server likely truncated it. "
                "Raise the model's context window (num_ctx) or this role's context_window.",
                type(self).__name__,
                response.input_tokens,
                window,
            )

    async def _generate(
        self,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResponse:
        """One provider call, retried on transient errors, each attempt under the deadline."""

        async def attempt() -> ProviderResponse:
            call = self._provider.generate(
                system_prompt=self._system_prompt,
                user_message=user_message,
                json_schema=json_schema,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            try:
                return await asyncio.wait_for(call, self._call_deadline)
            except TimeoutError:
                raise self._deadline_error() from None

        return cast(ProviderResponse, await retry_with_backoff(attempt))

    async def _within_deadline(self, stream: AsyncIterator[T]) -> AsyncIterator[T]:
        """Relay *stream*, failing when the next item takes longer than the call deadline."""
        items = aiter(stream)
        while True:
            try:
                item = await asyncio.wait_for(anext(items), self._call_deadline)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise self._deadline_error() from None
            yield item

    def _deadline_error(self) -> TimeoutError:
        return TimeoutError(
            f"{type(self).__name__}: no response from the provider within {self._call_deadline:g}s"
        )

    async def _prepare_call(
        self, input_data: dict[str, Any], json_schema: dict[str, Any]
    ) -> tuple[str, float, dict[str, Any] | None]:
        """Render the user message for one call, fitted to the context window.

        Works on a shallow copy of *input_data*, popping its ``_temperature``
        and ``_protected_files`` entries.  Returns the user message, the
        sampling temperature and the trim report (``None`` when nothing was
        trimmed).
        """
        data = dict(input_data)
        override = data.pop("_temperature", None)
        temperature = (
            self._temperature
            if override is None
            else clamp_temperature(override, self._temperature)
        )
        protected = _protected_paths(data, data.pop("_protected_files", None))

        context_window = await self._resolve_context_window()
        if context_window <= 0:
            return self._build_user_message(data), temperature, None

        budget = self._input_budget(context_window, json_schema)
        fit = fit_input_to_budget(
            data, self._build_user_message, budget, self._context_trim_keys, protected
        )
        self._list_dropped_files(fit.data, data, fit.dropped)
        user_message = self._build_user_message(fit.data)
        agent_name = type(self).__name__
        trim = fit.report(context_window, budget) if fit.trimmed else None
        if trim is not None:
            logger.warning(
                "%s: trimmed %s to fit the %d-token context window (dropped %s; excerpted %s)",
                agent_name,
                ", ".join(fit.trimmed),
                context_window,
                fit.dropped or "nothing",
                fit.excerpted or "nothing",
            )
        if fit.still_over:
            logger.warning(
                "%s: input (~%d tokens) exceeds its %d-token budget in a %d-token context window",
                agent_name,
                fit.tokens_after,
                budget,
                context_window,
            )
        return user_message, temperature, trim

    def _list_dropped_files(
        self, fitted: dict[str, Any], original: Mapping[str, Any], dropped: Mapping[str, list[str]]
    ) -> None:
        """List the files the guard dropped under ``_dropped_files_listing`` (path: bytes)."""
        for key, listing in self._dropped_files_listing.items():
            paths = dropped.get(key) or []
            if not paths:
                continue
            source = original.get(key) or {}
            listed = dict(fitted.get(listing) or {})
            for path in paths:
                content = source.get(path, "") if isinstance(source, Mapping) else ""
                listed.setdefault(path, len(str(content).encode("utf-8")))
            fitted[listing] = listed

    async def _resolve_context_window(self) -> int:
        """Return the context window: the configured one, else what the provider detects now.

        The provider caches its answer, so asking on every call is cheap; it
        lets a window re-read after the model loaded, or a server that was
        unreachable at first, take effect.
        """
        if self._configured_window > 0:
            return self._configured_window
        detect = getattr(self._provider, "detect_context_window", None)
        if detect is not None:
            self._context_window = int(await detect() or 0)
        return self._context_window

    @staticmethod
    def _usage_meta(
        attempts: list[ProviderResponse],
        temperature: float,
        trim: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the ``_meta`` usage record of a call.

        Token counts are totals over *attempts*; ``model`` and the request ids
        are the last attempt's, and ``attempts`` lists every attempt's own.
        """
        last = attempts[-1] if attempts else None
        meta: dict[str, Any] = {
            "model": last.model if last else "",
            "input_tokens": sum(r.input_tokens for r in attempts),
            "output_tokens": sum(r.output_tokens for r in attempts),
            "cache_read_tokens": sum(r.cache_read_tokens for r in attempts),
            "cache_write_tokens": sum(r.cache_write_tokens for r in attempts),
            "request_id": last.request_id if last else "",
            "provider_request_id": last.provider_request_id if last else "",
            "temperature": temperature,
            "attempts": [
                {
                    "model": r.model,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "request_id": r.request_id,
                    "provider_request_id": r.provider_request_id,
                    "finish_reason": r.finish_reason,
                }
                for r in attempts
            ],
        }
        if trim is not None:
            meta["context_trimmed"] = trim
        return meta

    def _parse_response(self, response: ProviderResponse, model: type[BaseModel]) -> dict[str, Any]:
        """Parse and validate provider response against a Pydantic model.

        Strips ``<think>`` reasoning blocks, then decodes each JSON object in
        the text, ignoring code fences and prose before or after it, and returns
        the first one that validates.  Objects nested inside a decoded object
        are never candidates, nor is an object that ends before the point where
        a surrounding malformed object failed to decode (it is part of that
        answer).  An object sharing no key with *model* (such as a dict literal
        inside a cut-off file) is used only when nothing else decodes.  Raises
        the first ``ValidationError``, else the first ``JSONDecodeError``, so
        the retry feedback names the real problem.
        """
        content = _THINK_BLOCK.sub("", response.content).strip()
        fields = set(model.model_fields)
        decoder = json.JSONDecoder()
        validation_error: ValidationError | None = None
        decode_error: json.JSONDecodeError | None = None
        keyless: list[dict[str, Any]] = []
        malformed_until = -1  # furthest failure position of a malformed object

        start = content.find("{")
        while start != -1:
            try:
                data, end = decoder.raw_decode(content, start)
            except json.JSONDecodeError as e:
                decode_error = decode_error or e
                malformed_until = max(malformed_until, e.pos)
                start = content.find("{", start + 1)
                continue
            inside_malformed = start < malformed_until and end <= malformed_until
            start = content.find("{", end)
            if inside_malformed:
                continue
            if not fields & data.keys():
                keyless.append(data)
                continue
            try:
                return model.model_validate(data).model_dump()
            except ValidationError as e:
                validation_error = validation_error or e

        if validation_error is not None:
            raise validation_error
        if decode_error is not None:
            raise decode_error
        if keyless:
            return model.model_validate(keyless[0]).model_dump()
        raise json.JSONDecodeError("No JSON object found", content, 0)


def _retry_prompt(name: str, values: Mapping[str, str]) -> str:
    """``prompts/<name>/v1.md`` with its ``{key}`` placeholders filled ("" when missing)."""
    try:
        text = load_prompt(name, version=1)
    except FileNotFoundError:
        logger.warning("Retry prompt %s/v1.md not found; using a plain retry note", name)
        return ""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text.strip()


def _protected_paths(data: Mapping[str, Any], explicit: Any) -> set[str]:
    """Files the context guard must keep: given ones, task outputs, failing tests, fix files."""
    paths = [p for p in explicit or [] if isinstance(p, str)]
    task = data.get("task")
    if isinstance(task, Mapping):
        paths.extend(p for p in task.get("outputs") or [] if isinstance(p, str))
    failing = data.get("failing_tests")
    ids = failed_test_ids(failing) if isinstance(failing, str) else []
    report = data.get("test_report")
    if isinstance(report, Mapping):
        ids.extend(i for i in report.get("failed_tests") or [] if isinstance(i, str))
    paths.extend(nodeid.split("::", 1)[0] for nodeid in ids)
    for key in _FEEDBACK_KEYS:
        items = data.get(key)
        for item in items if isinstance(items, list) else []:
            if isinstance(item, Mapping):
                paths.extend(item[k] for k in ("fix_file", "file") if isinstance(item.get(k), str))
    return {p for p in paths if p}
