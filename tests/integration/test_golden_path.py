"""Golden path integration tests — full CLI flows with mock providers.

Exercises the primary user journeys end-to-end through the CLI:
  1. `run` — full plan/code/test/review loop
  2. `plan` — architect-only plan generation
  3. `resume` — resuming from a saved checkpoint
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from delegatron.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a Click CLI test runner."""
    return CliRunner()


@pytest.fixture()
def mock_workspace(tmp_path: Path) -> Path:
    """Create a minimal git-initialised FastAPI project for testing.

    The workspace contains:
    - pyproject.toml with FastAPI dependency
    - A simple FastAPI app with one Pydantic model
    - An empty tests directory
    - One initial commit so the workspace is valid
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
    }

    # Initialise git repo
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

    # pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-app"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["fastapi", "pydantic"]\n'
    )

    # requirements.txt
    (tmp_path / "requirements.txt").write_text("fastapi\npydantic\nuvicorn\npytest\nhttpx\n")

    # Source tree
    src_dir = tmp_path / "src" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from pydantic import BaseModel\n\n"
        'app = FastAPI(title="Test App")\n\n\n'
        "class Item(BaseModel):\n"
        "    name: str\n"
        "    price: float\n\n\n"
        '@app.get("/health")\n'
        "def health() -> dict[str, str]:\n"
        '    return {"status": "ok"}\n'
    )

    # Tests directory
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")

    # Commit everything
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init: minimal FastAPI project"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        env=env,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Full pipeline run via CLI
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_run_full_pipeline_mock(runner: CliRunner, mock_workspace: Path) -> None:
    """Run the full pipeline via `main run --preset mock --mock` and verify success."""
    start = time.monotonic()

    result = runner.invoke(
        main,
        [
            "run",
            "--preset",
            "mock",
            "--mock",
            "--workspace",
            str(mock_workspace),
            "Add a CRUD endpoint for users",
        ],
        catch_exceptions=False,
    )

    elapsed = time.monotonic() - start

    assert result.exit_code == 0, f"CLI exited with code {result.exit_code}:\n{result.output}"

    # The stub sandbox passes every test run (tests/conftest.py): the mock plan's
    # t1 is approved; t2 stalls (the mock patch only applies once), so the run
    # is partial.
    output = result.output
    assert "PARTIAL" in output, f"Expected PARTIAL in output, got:\n{output}"
    assert "EXHAUSTED" not in output

    # Sanity-check that the run finished in a reasonable time (mock should be fast)
    assert elapsed < 120, f"Run took {elapsed:.1f}s — expected under 120s for mock providers"


@pytest.mark.integration
def test_run_json_output(runner: CliRunner, mock_workspace: Path) -> None:
    """Verify --json-output produces valid JSON with expected keys."""
    result = runner.invoke(
        main,
        [
            "run",
            "--preset",
            "mock",
            "--mock",
            "--json-output",
            "--workspace",
            str(mock_workspace),
            "Add a CRUD endpoint for users",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI exited with code {result.exit_code}:\n{result.output}"

    # JSON output is pretty-printed (indent=2) after possible JSONL log lines.
    # Find the state object by trying each '{' from the end.
    output = result.output.strip()
    state = None
    # Search backwards for the indented JSON state object
    pos = len(output)
    while pos > 0:
        pos = output.rfind("{", 0, pos)
        if pos < 0:
            break
        try:
            candidate = json.loads(output[pos:])
            if isinstance(candidate, dict) and "status" in candidate:
                state = candidate
                break
        except json.JSONDecodeError:
            pass
    assert state is not None, f"Could not find state JSON in output:\n{output[:500]}"
    assert state["status"] == "partial"
    assert state["plan"] is not None
    assert state["iteration"] == 4
    assert 0 <= state["total_cost_usd"] < 0.01


# ---------------------------------------------------------------------------
# 2. Plan-only flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_plan_only_mock(runner: CliRunner, mock_workspace: Path) -> None:
    """Run `main plan --preset mock` and verify a task table is displayed."""
    result = runner.invoke(
        main,
        [
            "plan",
            "--preset",
            "mock",
            "--workspace",
            str(mock_workspace),
            "Add users CRUD",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI exited with code {result.exit_code}:\n{result.output}"

    output = result.output
    # _display_plan renders a Rich Table with title "Tasks"
    assert "Tasks" in output or "Summary" in output, (
        f"Expected plan output with Tasks table, got:\n{output}"
    )


@pytest.mark.integration
def test_plan_json_output(runner: CliRunner, mock_workspace: Path) -> None:
    """Plan --json-output should return a valid JSON work plan."""
    result = runner.invoke(
        main,
        [
            "plan",
            "--preset",
            "mock",
            "--json-output",
            "--workspace",
            str(mock_workspace),
            "Add users CRUD",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI exited with code {result.exit_code}:\n{result.output}"

    # the plan is stdout; log lines (DELEGATRON_LOG_LEVEL=DEBUG) go to stderr
    plan = json.loads(result.stdout)
    assert "summary" in plan
    assert "tasks" in plan
    assert len(plan["tasks"]) > 0


# ---------------------------------------------------------------------------
# 3. Resume flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_resume_from_checkpoint(
    runner: CliRunner, mock_workspace: Path, passing_sandbox: list[dict]
) -> None:
    """An old-format checkpoint below the iteration cap resumes into an approved run (#89)."""
    run_id = "golden-resume-001"
    runs_dir = mock_workspace / "runs" / run_id
    runs_dir.mkdir(parents=True)

    # Write a minimal saved state that the resume command can load
    saved_state: dict = {
        "request": "Add a health endpoint",
        "status": "in_progress",
        "iteration": 1,
        "plan": {
            "summary": "Add GET /health endpoint",
            "tasks": [
                {
                    "task_id": "T1",
                    "title": "Implement health endpoint",
                    "risk_level": "low",
                    "depends_on": [],
                }
            ],
            "assumptions": [],
            "open_questions": [],
            "acceptance": {"criteria": ["GET /health returns 200"]},
        },
        "total_cost_usd": 0.001,
        "messages": [
            {"event": "architect_complete", "timestamp": "2026-01-01T00:00:00Z"},
        ],
        "review_report": None,
        "error": None,
        "project_info": {"languages": ["python"], "frameworks": ["fastapi"]},
    }
    (runs_dir / "state_001.json").write_text(json.dumps(saved_state, indent=2))

    result = runner.invoke(
        main,
        [
            "resume",
            run_id,
            "--mock",
            "--workspace",
            str(mock_workspace),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"CLI exited with code {result.exit_code}:\n{result.output}"

    output = result.output
    assert "Resuming" in output
    summary = json.loads((runs_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "approved", output
    assert summary["iterations"] > 1
    assert "APPROVED" in output
    assert "EXHAUSTED" not in output
    assert passing_sandbox, "the resumed run never tested its patch"
    checkpoints = sorted(runs_dir.glob("state_*.json"))
    assert json.loads(checkpoints[-1].read_text(encoding="utf-8"))["status"] == "approved"


@pytest.mark.integration
def test_resume_missing_checkpoint(runner: CliRunner, mock_workspace: Path) -> None:
    """Resume with a non-existent run ID should fail gracefully."""
    result = runner.invoke(
        main,
        [
            "resume",
            "nonexistent-run-id",
            "--mock",
            "--workspace",
            str(mock_workspace),
        ],
    )

    assert result.exit_code != 0, "Expected non-zero exit code for missing checkpoint"
    assert "No checkpoint found" in result.output or result.exit_code == 1


@pytest.fixture()
def passing_sandbox(sandbox_runs: list[dict]) -> list[dict]:
    """The patches the stub sandbox ran (every test run passes; tests/conftest.py)."""
    return sandbox_runs


def _task(task_id: str, title: str, depends_on: list[str]) -> dict:
    return {
        "task_id": task_id,
        "title": title,
        "description": title,
        "outputs": ["src/app/health.py"],
        "risk_level": "low",
        "depends_on": depends_on,
    }


@pytest.mark.integration
def test_resume_below_the_cap_finishes_the_task(
    runner: CliRunner, mock_workspace: Path, passing_sandbox: list[dict]
) -> None:
    """#89: a checkpoint below the iteration cap resumes into an approved run."""
    run_id = "golden-resume-approved"
    runs_dir = mock_workspace / "runs" / run_id
    runs_dir.mkdir(parents=True)
    task = _task("T1", "Implement health endpoint", [])
    saved_state: dict = {
        "request": "Add a health endpoint",
        "status": "running",
        "iteration": 1,
        "plan": {
            "summary": "Add GET /health endpoint",
            "tasks": [task],
            "acceptance": {"criteria": ["GET /health returns 200"]},
        },
        "all_tasks": [task],
        "current_task": task,
        "total_cost_usd": 0.001,
        "messages": [],
        "project_info": {"languages": ["python"], "frameworks": ["fastapi"]},
    }
    (runs_dir / "state_001.json").write_text(json.dumps(saved_state, indent=2))

    result = runner.invoke(main, ["resume", run_id, "--mock", "--workspace", str(mock_workspace)])

    assert result.exit_code == 0, result.output
    summary = json.loads((runs_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "approved", result.output
    assert "APPROVED" in result.output


@pytest.mark.integration
def test_resume_of_a_three_task_run_finishes_the_last_task(
    runner: CliRunner, mock_workspace: Path, passing_sandbox: list[dict]
) -> None:
    """#89: a resumed multi-task run carries on with its current task, not task 1."""
    run_id = "golden-resume-3-tasks"
    runs_dir = mock_workspace / "runs" / run_id
    runs_dir.mkdir(parents=True)
    tasks = [
        _task("T1", "Health module", []),
        _task("T2", "Uptime", ["T1"]),
        _task("T3", "Health tests", ["T2"]),
    ]
    saved_state: dict = {
        "request": "Add a health endpoint",
        "status": "running",
        "iteration": 7,
        "_task_iteration": 1,
        "plan": {
            "summary": "Add GET /health endpoint",
            "tasks": tasks,
            "acceptance": {"criteria": ["GET /health returns 200"]},
        },
        "all_tasks": tasks,
        "current_task": tasks[2],
        "_completed_tasks": ["T1", "T2"],
        "total_cost_usd": 0.001,
        "messages": [],
        "project_info": {"languages": ["python"], "frameworks": ["fastapi"]},
    }
    (runs_dir / "state_007.json").write_text(json.dumps(saved_state, indent=2))

    result = runner.invoke(main, ["resume", run_id, "--mock", "--workspace", str(mock_workspace)])

    assert result.exit_code == 0, result.output
    summary = json.loads((runs_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "approved", result.output
    assert summary["iterations"] > 7
    outcomes = {row["task_id"]: row["status"] for row in summary["task_outcomes"]}
    assert outcomes == {"T1": "approved", "T2": "approved", "T3": "approved"}
