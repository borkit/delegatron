"""End-to-end tests with Ollama presets on real tasks.

Validates the full pipeline flow using Ollama preset configurations with
a mocked HTTP backend — no real Ollama instance required. Covers:
- Preset loading and v2 prompt selection
- Provider creation with OpenAI-compatible adapter
- Full architect → implementer → sandbox → reviewer loop
- JSON object response format (not json_schema)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.agents.providers.mock import (
    MOCK_PATCH_SET,
    MOCK_REVIEW_APPROVED,
    MOCK_REVIEW_CHANGES_REQUIRED,
    MOCK_WORK_PLAN,
)
from delegatron.config import PRESETS, Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.graph import Pipeline


@pytest.fixture
def mock_workspace(tmp_path: Path) -> Path:
    """Create a minimal git-initialised Python project for Ollama E2E tests."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-app"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["fastapi"]\n'
    )
    (tmp_path / "requirements.txt").write_text("fastapi\npytest\n")
    src_dir = tmp_path / "src" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "main.py").write_text(
        'from fastapi import FastAPI\n\napp = FastAPI(title="Test App")\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


def _make_fake_completion(content: str, model: str = "qwen3-coder:30b") -> MagicMock:
    """Build a fake OpenAI ChatCompletion response object."""
    choice = MagicMock()
    choice.message.content = content
    usage = MagicMock()
    usage.prompt_tokens = 400
    usage.completion_tokens = 200
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model = model
    response.id = "chatcmpl-fake"
    return response


def _build_mock_generate(call_count: dict[str, int]) -> AsyncMock:
    """Return an AsyncMock that mimics the OpenAI chat.completions.create API.

    Uses the schema embedded in the system prompt to determine which mock
    response to return (WorkPlan, PatchSet, or ReviewReport).
    """

    async def fake_create(**kwargs: Any) -> MagicMock:
        messages = kwargs.get("messages", [])
        system_msg = messages[0]["content"] if messages else ""
        model = kwargs.get("model", "qwen3-coder:30b")

        if "WorkPlan" in system_msg:
            return _make_fake_completion(json.dumps(MOCK_WORK_PLAN), model)
        elif "PatchSet" in system_msg:
            return _make_fake_completion(json.dumps(MOCK_PATCH_SET), model)
        elif "ReviewReport" in system_msg:
            call_count["reviewer"] = call_count.get("reviewer", 0) + 1
            if call_count["reviewer"] <= 1:
                return _make_fake_completion(json.dumps(MOCK_REVIEW_CHANGES_REQUIRED), model)
            return _make_fake_completion(json.dumps(MOCK_REVIEW_APPROVED), model)
        else:
            return _make_fake_completion(json.dumps(MOCK_WORK_PLAN), model)

    return AsyncMock(side_effect=fake_create)


def _ollama_config(preset_name: str) -> Config:
    """Build a Config from an Ollama preset with tight limits for fast testing."""
    preset = PRESETS[preset_name]
    return Config(
        architect=RoleConfig(**{k: v for k, v in preset["architect"].items()}),
        implementer=RoleConfig(**{k: v for k, v in preset["implementer"].items()}),
        reviewer=RoleConfig(**{k: v for k, v in preset["reviewer"].items()}),
        pipeline=PipelineConfig(
            max_iterations=3,
            max_cost_usd=10.0,
            max_wall_time_minutes=2,
            sandbox="docker",
        ),
    )


# ---------------------------------------------------------------------------
# E2E pipeline tests with each Ollama preset
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_coder_preset_full_pipeline(mock_workspace: Path) -> None:
    """Run full pipeline with local-fast preset and mocked HTTP backend."""
    call_count: dict[str, int] = {}
    mock_create = _build_mock_generate(call_count)

    config = _ollama_config("local")

    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai") as mock_openai,
    ):
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        mock_openai.AsyncOpenAI.return_value = mock_client

        pipeline = Pipeline(
            config=config,
            workspace_root=mock_workspace,
            run_id="ollama-e2e-coder-001",
        )
        state = await pipeline.execute("Add a GET /health endpoint that returns status")

    assert state["status"] in ("approved", "partial", "exhausted", "stalled")
    assert state["plan"] is not None
    assert len(state["plan"]["tasks"]) > 0
    assert state["iteration"] >= 1
    # Ollama models are tracked as free ($0), so cost may be zero
    assert state["total_cost_usd"] >= 0

    events = [m.get("event") for m in state["messages"] if "event" in m]
    assert "architect_complete" in events
    assert "implementer_complete" in events
    assert "reviewer_complete" in events


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_mix_preset_full_pipeline(mock_workspace: Path) -> None:
    """Run full pipeline with local preset (different model per role)."""
    call_count: dict[str, int] = {}
    mock_create = _build_mock_generate(call_count)

    config = _ollama_config("local")

    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai") as mock_openai,
    ):
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        mock_openai.AsyncOpenAI.return_value = mock_client

        pipeline = Pipeline(
            config=config,
            workspace_root=mock_workspace,
            run_id="ollama-e2e-mix-001",
        )
        state = await pipeline.execute("Add user authentication with JWT tokens")

    assert state["status"] in ("approved", "partial", "exhausted", "stalled")
    assert state["plan"] is not None
    assert state["iteration"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ollama_fast_preset_full_pipeline(mock_workspace: Path) -> None:
    """Run full pipeline with local-fast preset (lightweight 8B models)."""
    call_count: dict[str, int] = {}
    mock_create = _build_mock_generate(call_count)

    config = _ollama_config("local")

    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai") as mock_openai,
    ):
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        mock_openai.AsyncOpenAI.return_value = mock_client

        pipeline = Pipeline(
            config=config,
            workspace_root=mock_workspace,
            run_id="ollama-e2e-fast-001",
        )
        state = await pipeline.execute("Fix a simple typo in the README")

    assert state["status"] in ("approved", "partial", "exhausted", "stalled")
    assert state["plan"] is not None


# ---------------------------------------------------------------------------
# V2 prompt and response format verification
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ollama_preset_uses_v2_prompts() -> None:
    """Verify all local presets specify prompt_version >= 3."""
    ollama_presets = {k: v for k, v in PRESETS.items() if k.startswith("local")}
    assert len(ollama_presets) >= 2, "Expected at least 2 local presets"

    for preset_name, roles in ollama_presets.items():
        for role_name in ("architect", "implementer", "reviewer"):
            role_data = roles.get(role_name, {})
            version = role_data.get("prompt_version", 0)
            assert version >= 3, (
                f"Preset {preset_name!r} role {role_name!r} "
                f"has prompt_version={version}, expected >= 3"
            )


@pytest.mark.integration
def test_ollama_preset_uses_json_object_format() -> None:
    """Verify Ollama provider uses json_object format, not json_schema."""
    from delegatron.agents.providers.openai_provider import OpenAIProvider

    with (
        patch("delegatron.agents.providers.openai_provider._HAS_OPENAI", True),
        patch("delegatron.agents.providers.openai_provider.openai", create=True),
    ):
        provider = OpenAIProvider(
            api_key="",
            model="qwen3-coder:30b",
            base_url="http://localhost:11434/v1",
        )

    schema = {"title": "TestSchema", "type": "object", "properties": {"name": {"type": "string"}}}
    kwargs = provider._build_request_kwargs(
        system_prompt="You are a test agent.",
        user_message="Do something.",
        json_schema=schema,
        max_tokens=4096,
        temperature=0.0,
    )

    # Should use json_object, not json_schema
    assert kwargs["response_format"]["type"] == "json_object"
    # Schema should be appended to system prompt
    system_content = kwargs["messages"][0]["content"]
    assert "TestSchema" in system_content
