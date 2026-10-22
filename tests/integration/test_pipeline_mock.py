"""End-to-end pipeline test using mock providers.

Tests the full orchestration loop without any API keys:
architect -> implementer -> sandbox -> reviewer -> route -> iterate
"""

import subprocess
from pathlib import Path

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig, load_config
from delegatron.orchestrator.graph import Pipeline


def _create_mock_config() -> Config:
    """Create a config using mock providers."""
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(
            max_iterations=5,
            max_cost_usd=10.0,
            max_wall_time_minutes=5,
            sandbox="docker",
        ),
    )


@pytest.fixture
def mock_workspace(tmp_path: Path) -> Path:
    """Create a minimal Python project for testing."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    # Init git repo
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

    # Create minimal project files
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-app"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["fastapi"]\n'
    )
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\npytest\nhttpx\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app").mkdir()
    (tmp_path / "src" / "app" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_full_pipeline_with_mock(mock_workspace: Path):
    """Run the full pipeline with mock providers and verify it completes."""
    config = _create_mock_config()

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id="test-001",
    )

    state = await pipeline.execute("Add a GET /health endpoint that returns status and uptime")

    # Pipeline should complete (either approved or exhausted after iterations)
    assert state["status"] in ("approved", "partial", "exhausted", "stalled")

    # Plan should have been created
    assert state["plan"] is not None
    assert state["plan"]["summary"] != ""
    assert len(state["plan"]["tasks"]) > 0

    # Should have run at least 1 iteration
    assert state["iteration"] >= 1

    # Cost should be tracked (mock/local models may be free, so >= 0)
    assert state["total_cost_usd"] >= 0

    # Messages should contain the audit trail
    events = [m.get("event") for m in state["messages"] if "event" in m]
    assert "architect_complete" in events
    assert "implementer_complete" in events
    assert "reviewer_complete" in events
    assert "pipeline_complete" in events

    # Final message should have summary
    final = state["messages"][-1]
    assert final["event"] == "pipeline_complete"
    assert "elapsed_seconds" in final


@pytest.mark.asyncio
async def test_pipeline_respects_iteration_limit(mock_workspace: Path):
    """Verify the pipeline stops at max_iterations."""
    config = Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        # Use a reviewer that always requires changes (call_count never > 1
        # because a new reviewer is created per pipeline, but the mock reviewer
        # approves on call 2, so with max_iterations=1 we should exhaust)
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(max_iterations=1, max_cost_usd=100.0),
    )

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id="test-002",
    )

    state = await pipeline.execute("Test iteration limit")

    # Should stop after 1 iteration (reviewer requests changes on first call,
    # but we hit max_iterations before the second iteration)
    assert state["iteration"] <= 2
    assert state["status"] in ("approved", "exhausted")


@pytest.mark.asyncio
async def test_pipeline_detects_project_info(mock_workspace: Path):
    """Verify project detection feeds into the pipeline state."""
    config = _create_mock_config()

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id="test-003",
    )

    state = await pipeline.execute("Test project detection")

    # Should have detected Python from pyproject.toml
    assert "python" in state["project_info"]["languages"]
    assert "fastapi" in state["project_info"]["frameworks"]


@pytest.mark.asyncio
async def test_mock_preset_from_config():
    """Verify the mock preset loads correctly."""
    config = load_config(preset="mock")

    assert config.architect.provider == "mock"
    assert config.implementer.provider == "mock"
    assert config.reviewer.provider == "mock"
