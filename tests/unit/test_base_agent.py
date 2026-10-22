"""Tests for agents/base.py — BaseAgent._parse_response, invoke, stream_invoke."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from delegatron.agents.base import (
    MAX_TRUNCATION_RETRY_TOKENS,
    MAX_VALIDATION_RETRIES,
    RETRY_RESERVE_TOKENS,
    AgentOutputError,
    BaseAgent,
    TruncatedOutputError,
)
from delegatron.agents.providers.base import (
    BaseProvider,
    ProviderRequestError,
    ProviderResponse,
    ProviderTransientError,
)
from delegatron.prompts import load_prompt, set_prompts_dir
from delegatron.utils.context_budget import estimate_tokens


@pytest.fixture
def agent_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """Capture ``delegatron.agents.base`` warnings whatever other tests did to logging.

    Server logging sets ``propagate=False`` on the ``delegatron`` logger, so
    caplog's root handler alone would miss these records when such a test ran
    first; the handler is attached to the module logger directly instead.
    """
    logger = logging.getLogger("delegatron.agents.base")
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=logger.name)
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


# ---------------------------------------------------------------------------
# Concrete test agent
# ---------------------------------------------------------------------------


class SampleOutput(BaseModel):
    title: str
    items: list[str] = []


class ConcreteAgent(BaseAgent):
    """Minimal concrete agent for testing."""

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        return input_data.get("message", "hello")

    def _get_output_schema(self) -> dict[str, Any]:
        return SampleOutput.model_json_schema()

    def _get_output_model(self) -> type[BaseModel]:
        return SampleOutput


def _make_response(content: str, model: str = "test-model") -> ProviderResponse:
    return ProviderResponse(
        content=content,
        model=model,
        input_tokens=10,
        output_tokens=20,
        request_id="req-1",
        provider_request_id="prov-1",
    )


# ---------------------------------------------------------------------------
# _parse_response tests
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Cover every repair strategy in _parse_response."""

    def _agent(self) -> ConcreteAgent:
        provider = MagicMock(spec=BaseProvider)
        return ConcreteAgent(provider=provider, system_prompt="test")

    def test_plain_json(self):
        agent = self._agent()
        resp = _make_response('{"title": "hello", "items": ["a"]}')
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "hello"
        assert result["items"] == ["a"]

    def test_strip_think_tags(self):
        agent = self._agent()
        content = '<think>reasoning here</think>{"title": "after_think", "items": []}'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "after_think"

    def test_strip_multiline_think_tags(self):
        agent = self._agent()
        content = '<think>\nlong\nreasoning\nblock\n</think>\n{"title": "multi", "items": []}'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "multi"

    def test_strip_markdown_fences(self):
        agent = self._agent()
        content = '```json\n{"title": "fenced", "items": []}\n```'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "fenced"

    def test_strip_markdown_fences_no_language(self):
        agent = self._agent()
        content = '```\n{"title": "nolang", "items": []}\n```'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "nolang"

    def test_json_embedded_in_prose(self):
        """JSON object buried in surrounding prose text."""
        agent = self._agent()
        content = 'Here is my answer:\n{"title": "embedded", "items": ["x"]}\nDone!'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "embedded"

    def test_iterative_scan_skips_bad_braces(self):
        """Iterative scan finds the valid JSON after an invalid '{'."""
        agent = self._agent()
        content = 'Some text {bad json here {"title": "found", "items": []}'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "found"

    def test_greedy_regex_fallback(self):
        """When iterative scan fails, greedy regex should still extract JSON."""
        agent = self._agent()
        # Trailing text after the JSON object causes json.loads to fail on
        # the full suffix, but the greedy regex can extract the object.
        content = 'prefix {"title": "regex", "items": []} trailing text {broken'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "regex"

    def test_invalid_json_raises(self):
        agent = self._agent()
        resp = _make_response("this is not json at all")
        with pytest.raises(json.JSONDecodeError):
            agent._parse_response(resp, SampleOutput)

    def test_valid_json_invalid_schema_raises(self):
        """Valid JSON but doesn't match the Pydantic model."""
        agent = self._agent()
        resp = _make_response('{"wrong_field": 123}')
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            agent._parse_response(resp, SampleOutput)

    def test_think_tags_plus_fences(self):
        """Combination: think tags wrapping markdown fences."""
        agent = self._agent()
        content = '<think>let me think</think>\n```json\n{"title": "combo", "items": []}\n```'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "combo"

    def test_whitespace_only_content_raises(self):
        agent = self._agent()
        resp = _make_response("   \n\n  ")
        with pytest.raises(json.JSONDecodeError):
            agent._parse_response(resp, SampleOutput)

    def test_multiple_think_tags_stripped(self):
        """Multiple think blocks should all be removed."""
        agent = self._agent()
        content = (
            "<think>first thought</think>"
            "some text "
            "<think>second thought</think>"
            '{"title": "multi_think", "items": []}'
        )
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "multi_think"

    def test_json_starts_with_brace_no_scan_needed(self):
        """Content that already starts with { should skip iterative scan."""
        agent = self._agent()
        resp = _make_response('{"title": "direct", "items": ["a", "b"]}')
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "direct"
        assert result["items"] == ["a", "b"]

    def test_json_preceded_by_prose_no_brace_before(self):
        """JSON preceded by prose with no brace before it."""
        agent = self._agent()
        content = 'The answer is: {"title": "answer", "items": []}'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "answer"

    def test_iterative_scan_finds_first_valid_object(self):
        """Should find the valid JSON via greedy regex when trailing text exists."""
        agent = self._agent()
        content = 'text {"title": "first_valid", "items": []} more text'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "first_valid"

    def test_greedy_regex_fallback_no_valid_json_from_scan(self):
        """When iterative scan fails on every '{', fallback regex is used."""
        agent = self._agent()
        content = 'intro {"title": "fallback_regex", "items": []} {unclosed'
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "fallback_regex"

    def test_content_with_leading_trailing_whitespace(self):
        """Leading and trailing whitespace should be stripped."""
        agent = self._agent()
        content = '  \n  {"title": "trimmed", "items": []}  \n  '
        resp = _make_response(content)
        result = agent._parse_response(resp, SampleOutput)
        assert result["title"] == "trimmed"

    @pytest.mark.parametrize(
        "content",
        [
            '{"title": "ok", "items": []}\n\nLet me know if you need changes.',
            '```json\n{"title": "ok", "items": []}\n```\n\nThe code looks fine.',
            '{"title": "ok", "items": []}{"title": "second", "items": []}',
            'Here: {"title": "ok", "items": []}\nNote: use {x} placeholders.',
            'Example: {"title": 5} Real answer: {"title": "ok", "items": []}',
        ],
        ids=["trailing-prose", "fence-then-prose", "concatenated", "prose-braces", "bad-example"],
    )
    def test_first_valid_object_wins_over_surrounding_text(self, content: str):
        result = self._agent()._parse_response(_make_response(content), SampleOutput)
        assert result["title"] == "ok"

    def test_invalid_object_raises_its_own_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            self._agent()._parse_response(_make_response('{"title": 5} trailing'), SampleOutput)
        assert "title" in str(exc_info.value)

    def test_nested_object_of_a_malformed_answer_is_not_accepted(self):
        """A trailing comma must be reported, not answered by an inner all-default object."""

        class AllDefaults(BaseModel):
            files: dict[str, str] = {}
            rationale: str = ""

        content = '{"files": {}, "plan": {"rationale": "inner"}, "rationale": "done",}'
        with pytest.raises(json.JSONDecodeError):
            self._agent()._parse_response(_make_response(content), AllDefaults)

    def test_object_without_known_keys_is_a_last_resort(self):
        content = '{"other": 1} {"title": "known", "items": []}'
        result = self._agent()._parse_response(_make_response(content), SampleOutput)
        assert result["title"] == "known"


# ---------------------------------------------------------------------------
# invoke tests
# ---------------------------------------------------------------------------


class TestInvoke:
    """Cover invoke success, retry, and exhaustion paths."""

    @pytest.mark.asyncio
    async def test_invoke_success(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_make_response('{"title": "ok", "items": []}'))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke({"message": "hi"})
        assert result["title"] == "ok"
        assert "_meta" in result
        assert result["_meta"]["model"] == "test-model"
        assert result["_meta"]["input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_invoke_retries_on_bad_json_then_succeeds(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[
                _make_response("not json"),
                _make_response('{"title": "retry_ok", "items": []}'),
            ]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke({"message": "hi"})
        assert result["title"] == "retry_ok"
        assert provider.generate.call_count == 2

    @pytest.mark.asyncio
    async def test_invoke_exhausts_retries(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_make_response("bad"))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        with pytest.raises(ValueError, match="failed to produce valid output"):
            await agent.invoke({"message": "hi"})
        assert provider.generate.call_count == 1 + MAX_VALIDATION_RETRIES

    @pytest.mark.asyncio
    async def test_invoke_retry_includes_error_in_message(self):
        """On retry, the user message should include the previous error."""
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[
                _make_response("bad json"),
                _make_response('{"title": "fixed", "items": []}'),
            ]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        await agent.invoke({"message": "original"})
        second_call_kwargs = provider.generate.call_args_list[1]
        user_msg = second_call_kwargs.kwargs.get("user_message", "")
        assert "IMPORTANT" in user_msg
        assert "invalid" in user_msg.lower()

    @pytest.mark.asyncio
    async def test_invoke_retries_on_validation_error_then_succeeds(self):
        """ValidationError from Pydantic should be retried in invoke loop."""
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[
                _make_response('{"wrong_field": 123}'),
                _make_response('{"title": "valid", "items": []}'),
            ]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke({"message": "hi"})
        assert result["title"] == "valid"
        assert provider.generate.call_count == 2

    @pytest.mark.asyncio
    async def test_invoke_meta_includes_all_fields(self):
        """Verify _meta dict includes all expected provider response fields."""
        provider = AsyncMock(spec=BaseProvider)
        resp = _make_response('{"title": "meta_test", "items": []}', model="gpt-4")
        resp.provider_request_id = "prov-xyz"
        provider.generate = AsyncMock(return_value=resp)
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke({"message": "hi"})
        meta = result["_meta"]
        assert meta["model"] == "gpt-4"
        assert meta["input_tokens"] == 10
        assert meta["output_tokens"] == 20
        assert meta["request_id"] == "req-1"
        assert meta["provider_request_id"] == "prov-xyz"

    @pytest.mark.asyncio
    async def test_invoke_last_error_in_raised_message(self):
        """The ValueError raised after exhausting retries should contain last error."""
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_make_response("not valid json"))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")
        with pytest.raises(ValueError, match="Last error"):
            await agent.invoke({"message": "hi"})


# ---------------------------------------------------------------------------
# stream_invoke tests
# ---------------------------------------------------------------------------


class TestStreamInvoke:
    @pytest.mark.asyncio
    async def test_stream_invoke_success(self):
        provider = AsyncMock(spec=BaseProvider)
        response = _make_response('{"title": "streamed", "items": []}')

        async def fake_stream(**kwargs):
            yield "token1"
            yield "token2"
            yield response

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = []
        async for item in agent.stream_invoke({"message": "hi"}):
            chunks.append(item)

        assert chunks[0] == "token1"
        assert chunks[1] == "token2"
        assert isinstance(chunks[2], dict)
        assert chunks[2]["title"] == "streamed"
        assert "_meta" in chunks[2]

    @pytest.mark.asyncio
    async def test_stream_invoke_no_response_raises(self):
        provider = AsyncMock(spec=BaseProvider)

        async def fake_stream(**kwargs):
            yield "token1"

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        with pytest.raises(ValueError, match="without a ProviderResponse"):
            async for _ in agent.stream_invoke({"message": "hi"}):
                pass

    @pytest.mark.asyncio
    async def test_stream_invoke_validation_failure_falls_back(self):
        """When stream output fails validation, falls back to non-streaming invoke."""
        provider = AsyncMock(spec=BaseProvider)
        bad_response = _make_response("not json")
        good_response = _make_response('{"title": "fallback", "items": []}')

        async def fake_stream(**kwargs):
            yield "chunk"
            yield bad_response

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        provider.generate = AsyncMock(return_value=good_response)

        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = []
        async for item in agent.stream_invoke({"message": "hi"}):
            chunks.append(item)

        assert chunks[0] == "chunk"
        assert isinstance(chunks[1], dict)
        assert chunks[1]["title"] == "fallback"

    @pytest.mark.asyncio
    async def test_stream_invoke_meta_attached(self):
        """Verify _meta is attached to the final artifact from stream."""
        provider = AsyncMock(spec=BaseProvider)
        response = _make_response('{"title": "meta_stream", "items": []}', model="claude-3")

        async def fake_stream(**kwargs):
            yield response

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = []
        async for item in agent.stream_invoke({"message": "hi"}):
            chunks.append(item)

        assert len(chunks) == 1
        assert isinstance(chunks[0], dict)
        assert chunks[0]["_meta"]["model"] == "claude-3"

    @pytest.mark.asyncio
    async def test_stream_invoke_only_provider_response(self):
        """Stream that yields only a ProviderResponse (no token chunks)."""
        provider = AsyncMock(spec=BaseProvider)
        response = _make_response('{"title": "no_tokens", "items": []}')

        async def fake_stream(**kwargs):
            yield response

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = []
        async for item in agent.stream_invoke({"message": "hi"}):
            chunks.append(item)

        assert len(chunks) == 1
        assert isinstance(chunks[0], dict)
        assert chunks[0]["title"] == "no_tokens"


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------


class InputEchoAgent(ConcreteAgent):
    """Renders its whole input, so tests can see exactly what reached the prompt."""

    def _build_user_message(self, input_data: dict[str, Any]) -> str:
        return json.dumps(input_data)


def _ok_provider() -> AsyncMock:
    provider = AsyncMock(spec=BaseProvider)
    provider.generate = AsyncMock(return_value=_make_response('{"title": "ok", "items": []}'))
    return provider


class TestTemperature:
    async def test_default_temperature_is_zero(self):
        provider = _ok_provider()
        await ConcreteAgent(provider=provider, system_prompt="sys").invoke({"message": "hi"})
        assert provider.generate.call_args.kwargs["temperature"] == 0.0

    async def test_configured_temperature_reaches_provider(self):
        provider = _ok_provider()
        agent = ConcreteAgent(provider=provider, system_prompt="sys", temperature=0.7)
        await agent.invoke({"message": "hi"})
        assert provider.generate.call_args.kwargs["temperature"] == 0.7

    async def test_per_call_override_is_popped_from_a_copy(self):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys", temperature=0.2)
        input_data = {"message": "hi", "_temperature": 0.4}
        await agent.invoke(input_data)
        kwargs = provider.generate.call_args.kwargs
        assert kwargs["temperature"] == 0.4
        assert "_temperature" not in kwargs["user_message"]
        assert input_data == {"message": "hi", "_temperature": 0.4}

    async def test_none_override_keeps_configured_temperature(self):
        provider = _ok_provider()
        agent = ConcreteAgent(provider=provider, system_prompt="sys", temperature=0.3)
        await agent.invoke({"message": "hi", "_temperature": None})
        assert provider.generate.call_args.kwargs["temperature"] == 0.3

    async def test_stream_invoke_passes_temperature(self):
        provider = AsyncMock(spec=BaseProvider)
        seen: dict[str, Any] = {}

        async def fake_stream(**kwargs):
            seen.update(kwargs)
            yield _make_response('{"title": "s", "items": []}')

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = InputEchoAgent(provider=provider, system_prompt="sys", temperature=0.1)
        async for _ in agent.stream_invoke({"message": "hi", "_temperature": 0.5}):
            pass
        assert seen["temperature"] == 0.5
        assert "_temperature" not in seen["user_message"]


# ---------------------------------------------------------------------------
# Context-window guard
# ---------------------------------------------------------------------------


def _large_context_input() -> dict[str, Any]:
    """About 40 KB of existing files, far beyond a 4096-token window."""
    return {
        "task": {"task_id": "t1", "title": "Deal cards"},
        "existing_files": {f"module_{i}.py": "x = 1\n" * 700 for i in range(10)},
    }


class TestContextWindowGuard:
    async def test_large_input_is_trimmed_to_fit_the_window(self, agent_log):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="SYSTEM", context_window=4096)
        result = await agent.invoke(_large_context_input())

        kwargs = provider.generate.call_args.kwargs
        assert kwargs["system_prompt"] == "SYSTEM"
        sent = json.loads(kwargs["user_message"])
        assert sent["task"] == {"task_id": "t1", "title": "Deal cards"}
        assert 0 < len(sent["existing_files"]) < 10
        schema_tokens = estimate_tokens(json.dumps(SampleOutput.model_json_schema(), indent=2))
        budget = 4096 - estimate_tokens("SYSTEM") - schema_tokens - 1024
        assert estimate_tokens(kwargs["user_message"]) <= budget - RETRY_RESERVE_TOKENS
        trim = result["_meta"]["context_trimmed"]
        assert trim["keys"] == ["existing_files"]
        assert trim["window"] == 4096
        assert trim["still_over"] is False
        assert trim["dropped"]["existing_files"]  # paths, least relevant first
        assert trim["dropped"]["existing_files"][0] == "module_9.py"
        assert "existing_files" in agent_log.text

    async def test_input_that_fits_is_not_trimmed(self):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=4096)
        result = await agent.invoke({"existing_files": {"a.py": "x = 1"}})
        assert json.loads(provider.generate.call_args.kwargs["user_message"]) == {
            "existing_files": {"a.py": "x = 1"}
        }
        assert "context_trimmed" not in result["_meta"]

    async def test_unknown_window_never_trims(self):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke(_large_context_input())
        sent = json.loads(provider.generate.call_args.kwargs["user_message"])
        assert len(sent["existing_files"]) == 10
        assert "context_trimmed" not in result["_meta"]

    async def test_window_detected_lazily_and_asked_per_call(self):
        # The provider caches its window; asking per call lets a window that
        # changes (re-read after load, a server back up) take effect.
        provider = _ok_provider()
        provider.detect_context_window = AsyncMock(side_effect=[0, 4096])
        agent = InputEchoAgent(provider=provider, system_prompt="sys")
        provider.detect_context_window.assert_not_awaited()

        first = await agent.invoke(_large_context_input())
        second = await agent.invoke(_large_context_input())

        assert provider.detect_context_window.await_count == 2
        assert "context_trimmed" not in first["_meta"]
        assert second["_meta"]["context_trimmed"]["keys"] == ["existing_files"]

    async def test_configured_window_skips_detection(self):
        provider = _ok_provider()
        provider.detect_context_window = AsyncMock(return_value=2048)
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=128000)
        result = await agent.invoke(_large_context_input())
        provider.detect_context_window.assert_not_awaited()
        assert "context_trimmed" not in result["_meta"]

    async def test_detected_zero_means_no_limit(self):
        provider = _ok_provider()
        provider.detect_context_window = AsyncMock(return_value=0)
        agent = InputEchoAgent(provider=provider, system_prompt="sys")
        result = await agent.invoke(_large_context_input())
        assert "context_trimmed" not in result["_meta"]

    async def test_warns_when_untrimmable_input_exceeds_budget(self, agent_log):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=2048)
        result = await agent.invoke({"request": "r" * 20000})
        assert "exceeds" in agent_log.text
        assert "context_trimmed" not in result["_meta"]

    async def test_warnings_are_captured_when_propagation_is_off(self, agent_log, monkeypatch):
        """Regression: server logging turns off propagation on the package logger."""
        monkeypatch.setattr(logging.getLogger("delegatron"), "propagate", False)
        agent = InputEchoAgent(provider=_ok_provider(), system_prompt="sys", context_window=2048)
        await agent.invoke({"request": "r" * 20000})
        assert "exceeds" in agent_log.text

    async def test_stream_invoke_reports_trimmed_keys(self):
        provider = AsyncMock(spec=BaseProvider)
        seen: dict[str, Any] = {}

        async def fake_stream(**kwargs):
            seen.update(kwargs)
            yield _make_response('{"title": "s", "items": []}')

        provider.stream_generate = MagicMock(side_effect=fake_stream)
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=4096)
        chunks = [item async for item in agent.stream_invoke(_large_context_input())]
        assert chunks[-1]["_meta"]["context_trimmed"]["keys"] == ["existing_files"]
        assert len(json.loads(seen["user_message"])["existing_files"]) < 10


# ---------------------------------------------------------------------------
# Truncated output (finish_reason == "length")
# ---------------------------------------------------------------------------

VALID = '{"title": "ok", "items": []}'


def _truncated(content: str = '{"title": "cut', output_tokens: int = 50) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        model="test-model",
        input_tokens=10,
        output_tokens=output_tokens,
        finish_reason="length",
    )


def _streaming_provider(*streamed: Any) -> AsyncMock:
    """A provider whose stream yields *streamed* items (an Exception item is raised)."""
    provider = AsyncMock(spec=BaseProvider)

    async def fake_stream(**kwargs):
        for item in streamed:
            if isinstance(item, Exception):
                raise item
            yield item

    provider.stream_generate = MagicMock(side_effect=fake_stream)
    return provider


class TestTruncation:
    async def test_always_truncated_output_stops_after_one_larger_retry(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_truncated(output_tokens=1000))
        agent = ConcreteAgent(provider=provider, system_prompt="sys", max_tokens=1000)

        with pytest.raises(TruncatedOutputError) as exc_info:
            await agent.invoke({"message": "hi"})

        assert isinstance(exc_info.value, ValueError)
        assert [c.kwargs["max_tokens"] for c in provider.generate.call_args_list] == [1000, 2000]
        message = str(exc_info.value)
        assert "1000 output tokens" in message
        assert "max_tokens=2000" in message
        assert exc_info.value.model == "test-model"

    async def test_truncated_response_is_never_accepted_even_if_it_parses(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[_truncated(VALID), _make_response('{"title": "full", "items": []}')]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        result = await agent.invoke({"message": "hi"})

        assert result["title"] == "full"
        retry_message = provider.generate.call_args_list[1].kwargs["user_message"]
        assert "cut off at the 16384-token output limit" in retry_message

    async def test_retry_budget_is_capped(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(side_effect=[_truncated(), _make_response(VALID)])
        agent = ConcreteAgent(provider=provider, system_prompt="sys", max_tokens=30000)

        await agent.invoke({"message": "hi"})

        retry_tokens = provider.generate.call_args_list[1].kwargs["max_tokens"]
        assert retry_tokens == MAX_TRUNCATION_RETRY_TOKENS

    async def test_meta_counts_tokens_of_every_attempt(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[
                _truncated(output_tokens=700),
                _make_response("bad"),
                _make_response(VALID),
            ]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        result = await agent.invoke({"message": "hi"})

        assert result["_meta"]["input_tokens"] == 30
        assert result["_meta"]["output_tokens"] == 700 + 20 + 20

    async def test_invalid_json_without_truncation_keeps_the_validation_path(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_make_response("bad"))
        agent = ConcreteAgent(provider=provider, system_prompt="sys", max_tokens=1000)

        with pytest.raises(ValueError, match="failed to produce valid output") as exc_info:
            await agent.invoke({"message": "hi"})

        assert not isinstance(exc_info.value, TruncatedOutputError)
        assert {c.kwargs["max_tokens"] for c in provider.generate.call_args_list} == {1000}
        assert provider.generate.call_count == 1 + MAX_VALIDATION_RETRIES

    async def test_truncated_stream_makes_one_extra_call(self):
        provider = _streaming_provider("tok", _truncated())
        provider.generate = AsyncMock(return_value=_truncated())
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        with pytest.raises(TruncatedOutputError):
            async for _ in agent.stream_invoke({"message": "hi"}):
                pass

        assert provider.generate.call_count == 1
        assert provider.generate.call_args.kwargs["max_tokens"] == MAX_TRUNCATION_RETRY_TOKENS


# ---------------------------------------------------------------------------
# Streaming fallbacks
# ---------------------------------------------------------------------------


class TestStreamFallback:
    async def test_invalid_stream_retries_with_the_parse_error(self):
        provider = _streaming_provider(_make_response("not json"))
        provider.generate = AsyncMock(return_value=_make_response(VALID))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = [item async for item in agent.stream_invoke({"message": "original"})]

        assert chunks[-1]["title"] == "ok"
        first_retry = provider.generate.call_args_list[0].kwargs["user_message"]
        assert first_retry.startswith("original")
        assert "No JSON object found" in first_retry
        assert chunks[-1]["_meta"]["input_tokens"] == 20  # streamed attempt + retry

    async def test_stream_counts_as_the_first_validation_attempt(self):
        provider = _streaming_provider(_make_response("bad"))
        provider.generate = AsyncMock(return_value=_make_response("bad"))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        with pytest.raises(ValueError, match="failed to produce valid output"):
            async for _ in agent.stream_invoke({"message": "hi"}):
                pass

        assert provider.generate.call_count == MAX_VALIDATION_RETRIES

    async def test_broken_stream_is_retried_without_streaming(self):
        provider = _streaming_provider("partial", ProviderTransientError("connection reset"))
        provider.generate = AsyncMock(return_value=_make_response(VALID))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        chunks = [item async for item in agent.stream_invoke({"message": "hi"})]

        assert chunks[0] == "partial"
        assert chunks[-1]["title"] == "ok"
        assert provider.generate.call_count == 1

    async def test_non_retryable_stream_error_propagates_after_one_call(self):
        provider = _streaming_provider(ProviderRequestError("401 invalid api key"))
        provider.generate = AsyncMock(return_value=_make_response(VALID))
        agent = ConcreteAgent(provider=provider, system_prompt="sys")

        with pytest.raises(ProviderRequestError):
            async for _ in agent.stream_invoke({"message": "hi"}):
                pass

        provider.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Call deadline (backstop for provider timeouts)
# ---------------------------------------------------------------------------


async def _hang(**kwargs: Any) -> ProviderResponse:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


class TestCallDeadline:
    def test_deadline_is_a_multiple_of_the_role_timeout(self):
        agent = ConcreteAgent(provider=_ok_provider(), system_prompt="sys", timeout_seconds=200)
        assert agent._call_deadline == 300

    def test_zero_timeout_disables_the_deadline(self):
        agent = ConcreteAgent(provider=_ok_provider(), system_prompt="sys", timeout_seconds=0)
        assert agent._call_deadline is None

    async def test_hung_generate_call_times_out(self, monkeypatch):
        async def single_attempt(fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        monkeypatch.setattr("delegatron.agents.base.retry_with_backoff", single_attempt)
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(side_effect=_hang)
        agent = ConcreteAgent(provider=provider, system_prompt="sys", timeout_seconds=0.02)

        with pytest.raises(TimeoutError, match="ConcreteAgent: no response from the provider"):
            await agent.invoke({"message": "hi"})

    async def test_stalled_stream_falls_back_to_a_plain_call(self):
        provider = AsyncMock(spec=BaseProvider)

        async def stalled_stream(**kwargs):
            yield "partial"
            await asyncio.Event().wait()

        provider.stream_generate = MagicMock(side_effect=stalled_stream)
        provider.generate = AsyncMock(return_value=_make_response(VALID))
        agent = ConcreteAgent(provider=provider, system_prompt="sys", timeout_seconds=0.02)

        chunks = [item async for item in agent.stream_invoke({"message": "hi"})]

        assert chunks[-1]["title"] == "ok"
        provider.generate.assert_awaited_once()


# ---------------------------------------------------------------------------
# #75: protected files, saturation backstop, retry reserve
# ---------------------------------------------------------------------------

_DEAL = "def deal(deck, n):\n" + "    return deck[:n]  # deal cards\n" * 400  # ~12.6 KB
_TEST_DEAL = "def test_deal():\n" + "    assert deal([1, 2], 1) == [1]\n" * 230  # ~8 KB


def _repair_input(**extra: Any) -> dict[str, Any]:
    """An attempt that must fix src/deal.py (task output) and its failing test."""
    return {
        "task": {"task_id": "t3", "outputs": ["src/deal.py"]},
        "failing_tests": "FAILED tests/test_deal.py::test_deal - AssertionError: 12 != 10",
        "existing_files": {
            "src/deal.py": _DEAL,
            "tests/test_deal.py": _TEST_DEAL,
            "src/util.py": "x = 1\n" * 400,  # 2.4 KB, unrelated
        },
        "repo_map": "src/util.py: x\n" * 300,
        **extra,
    }


class TestProtectedContext:
    async def test_files_under_repair_are_never_dropped(self):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=4096)

        result = await agent.invoke(_repair_input())

        sent = json.loads(provider.generate.call_args.kwargs["user_message"])
        assert set(sent["existing_files"]) == {"src/deal.py", "tests/test_deal.py"}
        assert "repo_map" not in sent
        trim = result["_meta"]["context_trimmed"]
        assert trim["keys"][0] == "repo_map"
        assert trim["dropped"] == {"existing_files": ["src/util.py"]}
        assert set(trim["excerpted"]["existing_files"]) <= {"src/deal.py", "tests/test_deal.py"}
        for path in trim["excerpted"]["existing_files"]:
            assert "... [truncated " in sent["existing_files"][path]

    async def test_feedback_and_explicit_files_are_protected(self):
        provider = _ok_provider()
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=4096)
        data = {
            "existing_files": {
                "a.py": "a = 1\n" * 900,
                "b.py": "b = 1\n" * 900,
                "c.py": "c = 1\n" * 900,
            },
            "review_feedback": [{"title": "x", "fix_file": "b.py"}],
            "_protected_files": ["c.py"],
        }

        result = await agent.invoke(data)

        sent = json.loads(provider.generate.call_args.kwargs["user_message"])
        assert "_protected_files" not in sent
        assert set(sent["existing_files"]) == {"b.py", "c.py"}
        assert result["_meta"]["context_trimmed"]["dropped"] == {"existing_files": ["a.py"]}

    async def test_input_budget_tokens_matches_the_guard(self):
        agent = InputEchoAgent(provider=_ok_provider(), system_prompt="SYSTEM", context_window=4096)
        schema_tokens = estimate_tokens(json.dumps(SampleOutput.model_json_schema(), indent=2))
        expected = 4096 - estimate_tokens("SYSTEM") - schema_tokens - 1024 - RETRY_RESERVE_TOKENS
        assert await agent.input_budget_tokens() == expected
        unknown = InputEchoAgent(provider=_ok_provider(), system_prompt="SYSTEM")
        assert await unknown.input_budget_tokens() == 0

    async def test_validation_retry_stays_inside_the_window(self):
        provider = AsyncMock(spec=BaseProvider)
        error = '{"wrong": "' + "x" * 5000 + '"}'  # a huge validation error
        provider.generate = AsyncMock(side_effect=[_make_response(error), _make_response(VALID)])
        agent = InputEchoAgent(provider=provider, system_prompt="sys", context_window=4096)

        await agent.invoke(_large_context_input())

        first, retry = (c.kwargs["user_message"] for c in provider.generate.call_args_list)
        schema_tokens = estimate_tokens(json.dumps(SampleOutput.model_json_schema(), indent=2))
        input_room = 4096 - estimate_tokens("sys") - schema_tokens - 1024
        assert estimate_tokens(retry) <= input_room
        assert retry.startswith(first)

    async def test_saturated_prompt_is_reported(self, agent_log):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            return_value=ProviderResponse(content=VALID, model="m", input_tokens=4090)
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys", context_window=4096)

        await agent.invoke({"message": "hi"})

        assert "server likely truncated" in agent_log.text
        assert "4090 of 4096" in agent_log.text

    async def test_prompt_below_the_window_is_not_reported(self, agent_log):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            return_value=ProviderResponse(content=VALID, model="m", input_tokens=3000)
        )
        agent = ConcreteAgent(provider=provider, system_prompt="sys", context_window=4096)
        await agent.invoke({"message": "hi"})
        assert "truncated" not in agent_log.text


# ---------------------------------------------------------------------------
# #76: temperature range, _meta and retries
# ---------------------------------------------------------------------------


class TestTemperatureRange:
    @pytest.mark.parametrize(("value", "expected"), [(5.0, 2.0), (-1, 0.0), ("abc", 0.0)])
    async def test_constructor_value_is_clamped(self, value, expected):
        provider = _ok_provider()
        await ConcreteAgent(provider=provider, system_prompt="s", temperature=value).invoke({})
        assert provider.generate.call_args.kwargs["temperature"] == expected

    @pytest.mark.parametrize(("value", "expected"), [(5.0, 2.0), (-1, 0.0), ("0.5", 0.5)])
    async def test_override_is_clamped(self, value, expected):
        provider = _ok_provider()
        agent = ConcreteAgent(provider=provider, system_prompt="s", temperature=0.2)
        await agent.invoke({"_temperature": value})
        assert provider.generate.call_args.kwargs["temperature"] == expected

    async def test_meta_reports_the_effective_temperature(self):
        agent = ConcreteAgent(provider=_ok_provider(), system_prompt="s", temperature=0.3)
        result = await agent.invoke({"_temperature": 0.9})
        assert result["_meta"]["temperature"] == 0.9

    async def test_invalid_then_valid_uses_the_same_temperature(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(side_effect=[_make_response("bad"), _make_response(VALID)])
        agent = ConcreteAgent(provider=provider, system_prompt="s", temperature=0.1)

        await agent.invoke({"_temperature": 0.6})

        assert [c.kwargs["temperature"] for c in provider.generate.call_args_list] == [0.6, 0.6]

    async def test_streamed_validation_failure_keeps_the_override(self):
        provider = _streaming_provider(_make_response("not json"))
        provider.generate = AsyncMock(return_value=_make_response(VALID))
        agent = ConcreteAgent(provider=provider, system_prompt="s", temperature=0.1)

        chunks = [item async for item in agent.stream_invoke({"_temperature": 0.7})]

        assert provider.stream_generate.call_args.kwargs["temperature"] == 0.7
        assert provider.generate.call_args.kwargs["temperature"] == 0.7
        assert chunks[-1]["_meta"]["temperature"] == 0.7


# ---------------------------------------------------------------------------
# #82: every attempt is accounted for
# ---------------------------------------------------------------------------


class TestAttemptUsage:
    async def test_meta_lists_every_attempt(self):
        provider = AsyncMock(spec=BaseProvider)
        bad = _make_response("bad")
        bad.request_id = "r1"
        good = _make_response(VALID)
        good.request_id = "r2"
        provider.generate = AsyncMock(side_effect=[bad, good])
        agent = ConcreteAgent(provider=provider, system_prompt="s")

        result = await agent.invoke({})

        attempts = result["_meta"]["attempts"]
        assert [a["request_id"] for a in attempts] == ["r1", "r2"]
        assert result["_meta"]["request_id"] == "r2"
        assert result["_meta"]["output_tokens"] == sum(a["output_tokens"] for a in attempts)

    async def test_failed_call_carries_its_usage(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_make_response("bad"))
        agent = ConcreteAgent(provider=provider, system_prompt="s")

        with pytest.raises(AgentOutputError) as exc_info:
            await agent.invoke({})

        meta = exc_info.value.meta
        assert len(meta["attempts"]) == 1 + MAX_VALIDATION_RETRIES
        assert meta["input_tokens"] == 10 * (1 + MAX_VALIDATION_RETRIES)

    async def test_truncation_error_carries_its_usage(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(return_value=_truncated(output_tokens=1000))
        agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=1000)

        with pytest.raises(TruncatedOutputError) as exc_info:
            await agent.invoke({})

        assert exc_info.value.meta["output_tokens"] == 2000
        assert [a["finish_reason"] for a in exc_info.value.meta["attempts"]] == ["length"] * 2


# ---------------------------------------------------------------------------
# #107: truncation retry prompt, budget and error paths
# ---------------------------------------------------------------------------


REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


@pytest.fixture
def repo_prompts():
    """Point the prompt loader at the repository's prompts/ (other tests repoint it)."""
    set_prompts_dir(REPO_PROMPTS)
    yield
    set_prompts_dir(REPO_PROMPTS)


class TestTruncationRetry:
    async def test_retry_note_comes_from_the_shipped_prompt(self, repo_prompts):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[_truncated(output_tokens=900), _make_response(VALID)]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=1000)

        await agent.invoke({"message": "orig"})

        retry = provider.generate.call_args_list[1].kwargs["user_message"]
        expected = (
            load_prompt("truncation_retry", version=1)
            .replace("{max_tokens}", "1000")
            .replace("{output_tokens}", "900")
            .strip()
        )
        assert retry == f"orig\n\n{expected}"
        assert "only the files you create or change" in retry
        assert "one line" in retry
        assert "{" not in expected

    async def test_retry_note_follows_the_prompts_dir(self, tmp_path: Path):
        (tmp_path / "truncation_retry").mkdir()
        (tmp_path / "truncation_retry" / "v1.md").write_text(
            "CUT at {output_tokens}/{max_tokens}", encoding="utf-8"
        )
        set_prompts_dir(tmp_path)
        try:
            provider = AsyncMock(spec=BaseProvider)
            provider.generate = AsyncMock(
                side_effect=[_truncated(output_tokens=7), _make_response(VALID)]
            )
            agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=8)
            await agent.invoke({"message": "m"})
        finally:
            set_prompts_dir(REPO_PROMPTS)

        assert provider.generate.call_args_list[1].kwargs["user_message"] == "m\n\nCUT at 7/8"

    def test_retry_prompts_ship_in_the_package(self):
        """pyproject maps prompts/ into the wheel, so the retry prompts ship with it."""
        pyproject = (REPO_PROMPTS.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert '"prompts" = "delegatron/prompts"' in pyproject
        for name in ("truncation_retry", "validation_retry"):
            assert (REPO_PROMPTS / name / "v1.md").is_file()

    async def test_retry_never_gets_less_budget_than_the_first_call(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(side_effect=[_truncated(), _make_response(VALID)])
        agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=64000)

        await agent.invoke({})

        assert [c.kwargs["max_tokens"] for c in provider.generate.call_args_list] == [64000, 64000]

    async def test_cut_on_the_last_attempt_is_a_truncation_error(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[_make_response("bad"), _make_response("bad"), _truncated(output_tokens=99)]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=1000)

        with pytest.raises(TruncatedOutputError) as exc_info:
            await agent.invoke({})

        assert exc_info.value.output_tokens == 99
        assert provider.generate.call_count == 1 + MAX_VALIDATION_RETRIES

    async def test_rejected_larger_budget_is_reported_as_truncation(self):
        provider = AsyncMock(spec=BaseProvider)
        provider.generate = AsyncMock(
            side_effect=[
                _truncated(output_tokens=16384),
                ProviderRequestError("400 max_tokens is too large: 32768"),
            ]
        )
        agent = ConcreteAgent(provider=provider, system_prompt="s", max_tokens=16384)

        with pytest.raises(TruncatedOutputError) as exc_info:
            await agent.invoke({})

        assert exc_info.value.max_tokens == 16384
        assert exc_info.value.output_tokens == 16384
        assert isinstance(exc_info.value.__cause__, ProviderRequestError)

    def test_message_at_the_cap_does_not_advise_raising_max_tokens(self):
        error = TruncatedOutputError("m", 32768, MAX_TRUNCATION_RETRY_TOKENS)
        assert "Raise this role's max_tokens" not in str(error)
        assert "Split the task" in str(error)


# ---------------------------------------------------------------------------
# #108: prose with a stray brace before the answer
# ---------------------------------------------------------------------------


class ReviewLike(BaseModel):
    summary: str
    acceptance_met: bool
    issues: list[dict[str, Any]] = []


class TestStrayBraceInProse:
    def _parse(self, content: str, model: type[BaseModel] = ReviewLike) -> dict[str, Any]:
        agent = ConcreteAgent(provider=MagicMock(spec=BaseProvider), system_prompt="s")
        return agent._parse_response(_make_response(content), model)

    def test_unterminated_brace_in_prose_does_not_hide_the_answer(self):
        content = 'The opener is {" here. {"summary":"real","acceptance_met":true,"issues":[]}'
        assert self._parse(content)["summary"] == "real"

    def test_nested_object_of_a_malformed_answer_is_still_rejected(self):
        content = '{"summary": "x", "acceptance_met": true, "issues": [{"summary": "y"}],}'
        with pytest.raises(json.JSONDecodeError):
            self._parse(content)
