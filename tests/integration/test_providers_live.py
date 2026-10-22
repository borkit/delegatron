"""Live provider integration tests — skipped when API keys are not set."""

from __future__ import annotations

import os

import pytest

from delegatron.agents.providers.registry import get_provider
from delegatron.config import RoleConfig

pytestmark = pytest.mark.integration


def _has_key(env_var: str) -> bool:
    val = os.environ.get(env_var, "")
    return bool(val) and not val.startswith("sk-ant-...")


skip_no_anthropic = pytest.mark.skipif(
    not _has_key("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
skip_no_openai = pytest.mark.skipif(
    not _has_key("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
skip_no_gemini = pytest.mark.skipif(
    not _has_key("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set",
)


def _make_config(provider: str, model: str) -> RoleConfig:
    return RoleConfig(provider=provider, model=model)


@skip_no_anthropic
@pytest.mark.asyncio
async def test_anthropic_simple_completion() -> None:
    """Verify Anthropic provider can complete a simple prompt."""
    config = _make_config("anthropic", "claude-sonnet-5-5")
    provider = get_provider(config)
    response = await provider.generate(
        system_prompt="You are a helpful assistant.",
        user_message="Say 'hello' and nothing else.",
        max_tokens=50,
    )
    assert "hello" in response.content.lower()
    assert response.input_tokens > 0


@skip_no_openai
@pytest.mark.asyncio
async def test_openai_simple_completion() -> None:
    """Verify OpenAI provider can complete a simple prompt."""
    config = _make_config("openai", "gpt-4o-mini")
    provider = get_provider(config)
    response = await provider.generate(
        system_prompt="You are a helpful assistant.",
        user_message="Say 'hello' and nothing else.",
        max_tokens=50,
    )
    assert "hello" in response.content.lower()
    assert response.input_tokens > 0


@skip_no_gemini
@pytest.mark.asyncio
async def test_gemini_simple_completion() -> None:
    """Verify Google Gemini provider can complete a simple prompt."""
    config = _make_config("google", "gemini-2.0-flash")
    provider = get_provider(config)
    response = await provider.generate(
        system_prompt="You are a helpful assistant.",
        user_message="Say 'hello' and nothing else.",
        max_tokens=50,
    )
    assert "hello" in response.content.lower()
    assert response.input_tokens > 0


@skip_no_anthropic
@pytest.mark.asyncio
async def test_anthropic_structured_output() -> None:
    """Verify Anthropic can return JSON-structured output."""
    config = _make_config("anthropic", "claude-sonnet-5-5")
    provider = get_provider(config)
    response = await provider.generate(
        system_prompt="You are a JSON generator. Always respond with valid JSON only.",
        user_message='Return a JSON object with key "status" and value "ok".',
        max_tokens=100,
    )
    assert '"status"' in response.content
    assert '"ok"' in response.content


@skip_no_openai
@pytest.mark.asyncio
async def test_openai_structured_output() -> None:
    """Verify OpenAI can return JSON-structured output."""
    config = _make_config("openai", "gpt-4o-mini")
    provider = get_provider(config)
    response = await provider.generate(
        system_prompt="You are a JSON generator. Always respond with valid JSON only.",
        user_message='Return a JSON object with key "status" and value "ok".',
        max_tokens=100,
    )
    assert '"status"' in response.content
    assert '"ok"' in response.content
