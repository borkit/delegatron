"""Release integration tests — critical paths for private alpha.

Covers:
  - Mid-pipeline error produces summary.json
  - Provider bootstrap failure → error summary
  - Cancelled run saves a checkpoint and cleans up
  - Full pipeline run writes all expected artifacts
  - An error after an approval clears is_approved

Every run uses the stub sandbox of tests/conftest.py (no Docker).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.graph import Pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_workspace(tmp_path: Path) -> Path:
    """Minimal git-backed Python project."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
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
# 3. Cancelled run checkpoints and cleans up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_run_saves_checkpoint(
    mock_workspace: Path, tmp_path: Path, sandbox_runs: list[dict[str, Any]]
) -> None:
    """A run cancelled mid-flight saves a checkpoint, cleans up and re-raises.

    The task is cancelled from the step callback once the plan is announced
    (``architect_complete``).  ``graph.Pipeline`` writes no summary for a
    cancelled run: the server does, from the ``CancelledError`` it re-raises.
    """
    config = _mock_config(max_iterations=10)
    artifacts = tmp_path / "runs"
    run_id = "cancel-001"
    steps: list[str] = []
    run: asyncio.Task[Any] | None = None

    def on_step(step: str, data: dict[str, Any]) -> None:
        steps.append(step)
        if step == "architect_complete" and run is not None:
            run.cancel()

    pipeline = Pipeline(
        config=config,
        workspace_root=mock_workspace,
        run_id=run_id,
        artifacts_dir=artifacts,
        step_callback=on_step,
    )

    with contextlib.ExitStack() as stack:
        gpu_cleanup = stack.enter_context(patch(f"{_GRAPH_MOD}.cleanup_after_run"))
        sandbox_cleanup = stack.enter_context(patch(f"{_GRAPH_MOD}._cleanup_sandbox"))
        run = asyncio.create_task(pipeline.execute("cancel me"))
        with pytest.raises(asyncio.CancelledError):
            await run

    assert "architect_complete" in steps
    assert "implementer_start" not in steps
    assert sandbox_runs == [], "a cancelled run went on to test a patch"
    gpu_cleanup.assert_called_once_with(config)
    sandbox_cleanup.assert_called_once()

    run_dir = artifacts / run_id
    checkpoints = sorted(run_dir.glob("state_*.json"))
    assert checkpoints, "no checkpoint saved for the cancelled run"
    saved = json.loads(checkpoints[-1].read_text(encoding="utf-8"))
    assert saved["status"] == "running"
    assert saved["plan"]["tasks"]
    assert not (run_dir / "summary.json").exists()


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

    # The mock reviewer approves task t1 on its second review; persisting that
    # approved review then fails.
    record_review = pipeline._record_review
    approved_before_error: list[bool] = []

    def _record_then_fail(state: Any, store: Any, *, emcs_override: bool) -> None:
        if state["is_approved"]:
            approved_before_error.append(True)
            raise RuntimeError("Kaboom")
        record_review(state, store, emcs_override=emcs_override)

    with patch.object(pipeline, "_record_review", side_effect=_record_then_fail):
        state = await pipeline.execute("break after approval")

    assert approved_before_error == [True], "the reviewer never approved before the failure"
    assert state["status"] == "error"
    assert "Kaboom" in state["error"]
    assert state["is_approved"] is False

    summary = json.loads((artifacts / run_id / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "error"
    assert summary["is_approved"] is False
