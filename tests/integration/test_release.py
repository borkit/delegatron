"""Release integration tests — critical paths for private alpha.

Covers:
  - Mid-pipeline error produces summary.json
  - Provider bootstrap failure → error summary
  - Cancelled run persists summary.json
  - Full pipeline run writes all expected artifacts
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.async_runner import AsyncPipeline, PipelineStatus
from delegatron.orchestrator.graph import Pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_workspace(tmp_path: Path) -> Path:
    """Minimal git-backed Python project."""
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
        '[project]\nname = "test-app"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


def _mock_config(max_iterations: int = 2) -> Config:
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(
            max_iterations=max_iterations,
            max_cost_usd=10.0,
            max_wall_time_minutes=5,
            sandbox="docker",
        ),
    )


# ---------------------------------------------------------------------------
# 1. Mid-pipeline error produces summary.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_pipeline_error_produces_summary(mock_workspace: Path, tmp_path: Path) -> None:
    """When the implementer throws mid-run, summary.json is still written."""
    config = _mock_config()
    artifacts = tmp_path / "runs"
    run_id = "err-mid-001"

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id=run_id,
        artifacts_dir=artifacts,
    )

    # Make the implementer blow up after the architect succeeds
    async def _exploding_implementer(state: Any, impl: Any, tracker: Any) -> Any:
        raise RuntimeError("Simulated mid-pipeline crash")

    with patch.object(pipeline, "_run_implementer", side_effect=_exploding_implementer):
        state = await pipeline.execute("trigger error")

    assert state["status"] == "error"
    assert state["is_approved"] is False

    # The critical check: summary.json must exist
    summary_path = artifacts / run_id / "summary.json"
    assert summary_path.exists(), "summary.json missing after mid-pipeline error"

    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "error"
    assert summary["run_id"] == run_id
    assert "error" in summary


# ---------------------------------------------------------------------------
# 2. Provider bootstrap failure → error summary
# ---------------------------------------------------------------------------

_GRAPH_MOD = "delegatron.orchestrator.graph"


@pytest.mark.asyncio
async def test_provider_bootstrap_failure_produces_summary(
    mock_workspace: Path, tmp_path: Path
) -> None:
    """If the architect provider fails to initialise, we still get a summary."""
    config = _mock_config()
    artifacts = tmp_path / "runs"
    run_id = "err-boot-001"

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id=run_id,
        artifacts_dir=artifacts,
    )

    # Make _create_architect explode (simulates API key missing / provider down)
    with patch.object(
        pipeline,
        "_create_architect",
        side_effect=RuntimeError("Provider bootstrap failed"),
    ):
        state = await pipeline.execute("trigger bootstrap error")

    assert state["status"] == "error"
    assert state["is_approved"] is False

    summary_path = artifacts / run_id / "summary.json"
    assert summary_path.exists(), "summary.json missing after bootstrap failure"

    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "error"


# ---------------------------------------------------------------------------
# 3. Cancelled async run persists summary.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_run_saves_summary(mock_workspace: Path, tmp_path: Path) -> None:
    """AsyncPipeline cancel produces summary.json with status=cancelled."""
    config = _mock_config(max_iterations=10)
    artifacts = tmp_path / "runs"
    run_id = "cancel-001"

    _MOD = "delegatron.orchestrator.async_runner"

    # Set up mocks for external deps
    proj = MagicMock()
    proj.languages = ["python"]
    proj.frameworks = []
    proj.test_commands = ["pytest"]
    proj.lint_commands = []
    proj.package_manager = "pip"

    ws = MagicMock()
    ws.get_file_tree.return_value = ["src/__init__.py"]
    ws.initialize.return_value = MagicMock(branch="run-x", commit_sha="deadbeef1234")
    ws.apply_patch.return_value = True
    ws.commit.return_value = "abc12345"

    sandbox_inst = AsyncMock()
    sandbox_inst.apply_and_test = AsyncMock(
        return_value={"passed": True, "total": 1, "failures": 0}
    )

    arch = MagicMock()
    arch.invoke = AsyncMock(
        return_value={
            "summary": "Plan",
            "tasks": [{"task_id": "t1", "title": "Do"}],
            "_meta": {"model": "m", "input_tokens": 10, "output_tokens": 10},
        }
    )
    impl = MagicMock()
    impl.invoke = AsyncMock(
        return_value={
            "status": "ok",
            "rationale": "Done",
            "diff_unified": "",
            "_meta": {"model": "m", "input_tokens": 10, "output_tokens": 10},
        }
    )
    rev = MagicMock()
    rev.invoke = AsyncMock(
        return_value={
            "acceptance_met": False,
            "issues": [{"severity": "minor", "title": "nit"}],
            "_meta": {"model": "m", "input_tokens": 10, "output_tokens": 10},
        }
    )

    patches = [
        patch(f"{_MOD}.detect_project", return_value=proj),
        patch(f"{_MOD}.Workspace", return_value=ws),
        patch(f"{_MOD}.create_sandbox", return_value=sandbox_inst),
        patch(f"{_MOD}.create_architect", return_value=arch),
        patch(f"{_MOD}.create_implementer", return_value=impl),
        patch(f"{_MOD}.create_reviewer", return_value=rev),
        patch(f"{_MOD}.CostTracker"),
        patch(f"{_MOD}.gather_context", return_value={}),
        patch(f"{_MOD}.should_terminate", return_value=False),
        patch(f"{_MOD}.detect_stall", return_value=False),
        patch(f"{_MOD}.get_score_summary", return_value={"score": 0.5}),
        patch(f"{_MOD}.should_accept", return_value=False),
        patch("delegatron.orchestrator.router.route_after_review", return_value="implementer"),
        patch("delegatron.security.secrets.scan_patch", return_value=[]),
    ]
    for p in patches:
        p.__enter__()

    try:
        pipeline = AsyncPipeline(
            config=config,
            workspace_root=mock_workspace,
            run_id=run_id,
            artifacts_dir=artifacts,
        )

        async def cancel_after_plan() -> None:
            async for ev in pipeline.subscribe():
                if ev.event_type == "plan_created":
                    pipeline.cancel()

        task = asyncio.create_task(cancel_after_plan())
        await asyncio.sleep(0)
        state = await pipeline.execute("cancel me")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        for p in patches:
            p.__exit__(None, None, None)

    assert state["status"] == "cancelled"
    assert pipeline.status() == PipelineStatus.CANCELLED

    # The critical check: summary.json must exist even for cancelled runs
    summary_path = artifacts / run_id / "summary.json"
    assert summary_path.exists(), "summary.json missing after cancellation"

    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. Full pipeline run writes all expected artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_run_writes_artifacts(mock_workspace: Path, tmp_path: Path) -> None:
    """A complete mock pipeline run produces all expected artifact files."""
    config = _mock_config(max_iterations=2)
    artifacts = tmp_path / "runs"
    run_id = "full-001"

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id=run_id,
        artifacts_dir=artifacts,
    )

    await pipeline.execute("Add health endpoint")

    run_dir = artifacts / run_id

    # Must-have artifacts
    assert (run_dir / "summary.json").exists(), "Missing summary.json"
    assert (run_dir / "events.jsonl").exists(), "Missing events.jsonl"
    assert (run_dir / "work_plan.json").exists(), "Missing work_plan.json"

    # Summary must be valid JSON with required fields
    summary = json.loads((run_dir / "summary.json").read_text())
    for field in ("run_id", "status", "iterations", "total_cost_usd", "is_approved"):
        assert field in summary, f"Missing field '{field}' in summary"

    # Events file must have at least start + complete
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    event_types = [e.get("event") for e in events]
    assert "pipeline_start" in event_types
    assert "pipeline_complete" in event_types

    # State checkpoint must exist
    state_files = list(run_dir.glob("state_*.json"))
    assert len(state_files) >= 1, "No state checkpoint saved"

    # At least one iteration's artifacts
    assert list(run_dir.glob("patch_set_*.json")), "No patch artifact saved"
    assert list(run_dir.glob("test_report_*.json")), "No test report saved"
    assert list(run_dir.glob("review_report_*.json")), "No review artifact saved"


# ---------------------------------------------------------------------------
# 5. Error state always has is_approved=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_clears_is_approved(mock_workspace: Path, tmp_path: Path) -> None:
    """is_approved must be False when status is 'error', even if reviewer
    previously approved."""
    config = _mock_config()
    artifacts = tmp_path / "runs"
    run_id = "err-approved-001"

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id=run_id,
        artifacts_dir=artifacts,
    )

    # Architect succeeds, but then we blow up in the implementer
    with patch.object(
        pipeline,
        "_run_implementer",
        side_effect=RuntimeError("Kaboom"),
    ):
        state = await pipeline.execute("break after plan")

    assert state["status"] == "error"
    assert state["is_approved"] is False

    summary = json.loads((artifacts / run_id / "summary.json").read_text())
    assert summary["is_approved"] is False
