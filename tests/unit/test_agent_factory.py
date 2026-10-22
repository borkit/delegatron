"""Tests for factories/agent_factory.py — role settings reach the agents."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from delegatron.agents.director import DirectorAgent
from delegatron.agents.implementer import ImplementerAgent
from delegatron.config import Config, RoleConfig, load_config
from delegatron.factories import agent_factory

_MOD = "delegatron.factories.agent_factory"


@pytest.fixture
def load_prompt():
    with patch(f"{_MOD}.load_prompt", return_value="PROMPT") as mock:
        yield mock


def _config() -> Config:
    return Config(
        architect=RoleConfig(
            provider="mock",
            model="mock-architect",
            prompt_version=5,
            temperature=0.1,
            context_window=8192,
        ),
        implementer=RoleConfig(
            provider="mock",
            model="mock-implementer",
            prompt_version=5,
            max_tokens=4096,
            temperature=0.7,
            context_window=16384,
        ),
    )


def test_role_settings_reach_the_agent(load_prompt):
    agent = agent_factory.create_implementer(_config())

    assert isinstance(agent, ImplementerAgent)
    load_prompt.assert_called_once_with("implementer", version=5)
    assert agent._system_prompt == "PROMPT"
    assert agent._max_tokens == 4096
    assert agent._temperature == 0.7
    assert agent._context_window == 16384
    assert agent._call_deadline == 1.5 * _config().implementer.timeout_seconds


def test_director_uses_architect_role_with_its_own_prompt(load_prompt):
    with patch(f"{_MOD}.get_provider", return_value=MagicMock()) as get_provider:
        agent = agent_factory.create_director(_config())

    assert isinstance(agent, DirectorAgent)
    get_provider.assert_called_once_with(_config().architect)
    load_prompt.assert_called_once_with("director", version=agent_factory.DIRECTOR_PROMPT_VERSION)
    assert agent_factory.DIRECTOR_PROMPT_VERSION == 1
    assert agent._temperature == 0.1
    assert agent._context_window == 8192


def test_local_agents_do_not_detect_context_window_at_creation(load_prompt):
    """Detection is lazy: creating an agent for a local endpoint sends no request."""
    local = RoleConfig(
        provider="openai-compatible",
        model="qwen2.5-coder:14b",
        base_url="http://localhost:11434/v1",
        prompt_version=5,
    )
    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai", create=True) as openai,
    ):
        agent = agent_factory.create_implementer(Config(implementer=local))

    assert agent._context_window == 0
    assert agent._provider._context_window is None
    openai.AsyncOpenAI.return_value.with_options.assert_not_called()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (RoleConfig(provider="anthropic", model="claude-sonnet-4-20250514"), 1),
        (RoleConfig(provider="openai", model="gpt-4.1"), 1),
        (RoleConfig(provider="google", model="gemini-2.5-pro"), 1),
        (RoleConfig(provider="openai-compatible", model="m", base_url="https://api.x.ai/v1"), 1),
        (
            RoleConfig(
                provider="openai-compatible", model="m", base_url="http://localhost:11434/v1"
            ),
            agent_factory.LOCAL_PROMPT_VERSION,
        ),
        (RoleConfig(provider="mock", model="mock-implementer"), agent_factory.LOCAL_PROMPT_VERSION),
        # #111: a LAN Ollama host is local, like zero-cloud says (one shared helper)
        (
            RoleConfig(
                provider="openai-compatible", model="m", base_url="http://192.168.1.5:11434/v1"
            ),
            agent_factory.LOCAL_PROMPT_VERSION,
        ),
        (
            RoleConfig(provider="openai-compatible", model="m", base_url="http://gpu.lan:8000/v1"),
            agent_factory.LOCAL_PROMPT_VERSION,
        ),
        (RoleConfig(provider="anthropic", model="m", prompt_version=4), 4),
        (RoleConfig(provider="openai-compatible", model="m", prompt_version=2), 2),
    ],
)
def test_resolve_prompt_version(role: RoleConfig, expected: int):
    assert agent_factory.resolve_prompt_version(role) == expected


def test_temperature_is_clamped_and_logged(load_prompt, caplog):
    """#76: the agent gets a temperature in [0, 2] and the debug log shows it."""
    config = Config(implementer=RoleConfig(provider="mock", model="m", temperature=5.0))
    logger = agent_factory.logger
    logger.addHandler(caplog.handler)
    caplog.set_level("DEBUG", logger=logger.name)
    try:
        agent = agent_factory.create_implementer(config)
    finally:
        logger.removeHandler(caplog.handler)

    assert agent._temperature == 2.0
    assert "temp=2.00" in caplog.text


@pytest.mark.parametrize(
    ("preset", "expected"), [(None, 1), ("default", 1), ("local", 5), ("local-oss", 5)]
)
def test_unpinned_and_preset_roles_load_the_matching_prompt(
    load_prompt, tmp_path, monkeypatch, preset, expected
):
    monkeypatch.chdir(tmp_path)
    config = load_config(preset=preset)
    with patch(f"{_MOD}.get_provider", return_value=MagicMock()):
        for create in (
            agent_factory.create_architect,
            agent_factory.create_implementer,
            agent_factory.create_reviewer,
        ):
            create(config)

    assert [c.kwargs["version"] for c in load_prompt.call_args_list] == [expected] * 3


@pytest.mark.parametrize(
    ("create", "role", "output"),
    [
        (
            agent_factory.create_architect,
            "architect",
            '{"summary": "s", "tasks": [{"task_id": "t1", "title": "t", "description": "d",'
            ' "outputs": ["a.py"]}], "acceptance": {"criteria": ["c"], "tests": []}}',
        ),
        (agent_factory.create_implementer, "implementer", '{"files": {"a.py": "x = 1"}}'),
        (agent_factory.create_reviewer, "reviewer", '{"summary": "ok", "acceptance_met": true}'),
    ],
)
async def test_each_role_sends_its_temperature(load_prompt, create, role, output):
    """#76: the configured temperature reaches the provider request of every role."""
    from delegatron.agents.providers.base import ProviderResponse

    sent: list[float] = []

    class _Recording:
        async def generate(self, **kwargs):  # type: ignore[no-untyped-def]
            sent.append(kwargs["temperature"])
            return ProviderResponse(content=output, model="m")

    role_config = RoleConfig(provider="mock", model="m", prompt_version=5, temperature=0.7)
    config = Config(**{role: role_config})
    with patch(f"{_MOD}.get_provider", return_value=_Recording()):
        agent = create(config)

    await agent.invoke({"request": "r"})

    assert sent == [0.7]
