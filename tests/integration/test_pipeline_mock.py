"""End-to-end pipeline test using mock providers.

Tests the full orchestration loop without any API keys:
architect -> implementer -> sandbox -> reviewer -> route -> iterate
"""

import os
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


def _outcomes(state: dict) -> dict[str, tuple[str, int]]:
    """Each task's outcome status and the iterations it took."""
    return {t: (row["status"], row["iterations"]) for t, row in state["_task_outcomes"].items()}


@pytest.fixture
def mock_workspace(tmp_path: Path) -> Path:
    """Create a minimal Python project for testing."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
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
async def test_full_pipeline_with_mock(mock_workspace: Path, sandbox_runs: list[dict]):
    """Run the full pipeline with mock providers and the stub sandbox (tests/conftest.py)."""
    config = _create_mock_config()

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id="test-001",
    )

    state = await pipeline.execute("Add a GET /health endpoint that returns status and uptime")

    # Every stub test run passes.  t1 is approved on its first pass (the mock
    # reviewer's one issue is minor); the mock implementer's patch creates
    # src/app/health.py again, so it never applies to t2, which stalls.
    assert state["status"] == "partial", state.get("_termination_reason")
    assert state["is_approved"] is False
    assert _outcomes(state) == {"t1": ("approved", 1), "t2": ("stalled", 3)}

    # Plan should have been created
    assert state["plan"] is not None
    assert state["plan"]["summary"] != ""
    assert len(state["plan"]["tasks"]) > 0

    assert state["iteration"] == 4
    assert sandbox_runs, "the stub sandbox never ran a patch"

    # Mock models have no API cost (a local GPU estimate may add a fraction of a cent)
    assert 0 <= state["total_cost_usd"] < 0.01

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
    """Verify each task stops at max_iterations (a per-task limit)."""
    config = Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(max_iterations=1, max_cost_usd=100.0),
    )

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id="test-002",
    )

    state = await pipeline.execute("Test iteration limit")

    # One iteration per task: t1 is approved on it; t2's patch never applies
    # and the limit leaves it no second attempt.
    assert state["iteration"] == 2
    assert state["status"] == "partial", state.get("_termination_reason")
    assert _outcomes(state) == {"t1": ("approved", 1), "t2": ("exhausted", 1)}


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
