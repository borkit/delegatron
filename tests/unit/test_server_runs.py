"""Server run lifecycle: files across iterations, restore, delete, cleanup, finalization."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import delegatron.server.app as app_module
from delegatron.config import load_config
from delegatron.integrations.git_pr import PRResult
from delegatron.orchestrator import graph
from delegatron.server.app import app, configure
from delegatron.workspace import Workspace, WorkspaceInitError

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DELEGATRON_AUTH_ENABLED", "false")
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    return TestClient(app)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _patch(run_dir: Path, iteration: int, files: dict[str, str]) -> None:
    _write_json(run_dir / f"patch_set_{iteration:03d}.json", {"files": files, "diff_unified": ""})


# --- Files across iterations -------------------------------------------------


def _two_task_run(tmp_path: Path, run_id: str = "two-task") -> Path:
    run_dir = tmp_path / "runs" / run_id
    _write_json(run_dir / "summary.json", {"run_id": run_id, "status": "approved", "request": "x"})
    _patch(run_dir, 1, {"card_deck.py": "deck\n", "tests/test_card_deck.py": "t1\n"})
    _patch(run_dir, 3, {"tests/test_card_deck.py": "t3\n"})
    _patch(run_dir, 4, {"simulate_deal.py": "deal\n", "tests/test_simulate_deal.py": "td\n"})
    return run_dir


ALL_FILES = [
    "card_deck.py",
    "simulate_deal.py",
    "tests/test_card_deck.py",
    "tests/test_simulate_deal.py",
]


def _zip_names(resp: Any) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())


def test_every_endpoint_serves_files_from_all_iterations(
    client: TestClient, tmp_path: Path
) -> None:
    _two_task_run(tmp_path)

    download = client.get("/api/runs/two-task/download")
    files = client.get("/api/runs/two-task/files").json()["files"]
    summary = client.get("/api/runs/two-task").json()

    assert _zip_names(download) == {*ALL_FILES, "DELEGATRON_RUN.md"}
    assert sorted(files) == ALL_FILES
    assert files["tests/test_card_deck.py"] == "t3\n"
    assert summary["files_created"] == ALL_FILES


def test_a_test_only_fix_keeps_the_module_in_the_download(
    client: TestClient, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / "single"
    _write_json(run_dir / "summary.json", {"run_id": "single", "status": "approved"})
    _patch(run_dir, 1, {"mod.py": "m\n", "tests/test_mod.py": "t1\n"})
    _patch(run_dir, 2, {"tests/test_mod.py": "t2\n"})

    assert "mod.py" in _zip_names(client.get("/api/runs/single/download"))


def test_files_of_a_run_without_patches_is_empty(client: TestClient, tmp_path: Path) -> None:
    _write_json(tmp_path / "runs" / "early" / "summary.json", {"status": "error"})

    assert client.get("/api/runs/early/files").json() == {"files": {}}


@requires_git
def test_files_are_read_from_the_run_workspace_while_it_exists(
    client: TestClient, tmp_path: Path
) -> None:
    base = tmp_path / "project"
    _write(base / "untouched.py", "same\n")
    workspace = base / ".delegatron-runs" / "disk-run"
    init_sha = app_module._init_run_workspace(base, workspace, "disk-run")
    _write(workspace / "card_deck.py", "on disk\n")
    run_dir = _two_task_run(tmp_path, "disk-run")
    meta = {"run_workspace": str(workspace), "workspace_init_sha": init_sha}
    _write_json(run_dir / "meta.json", meta)

    files = client.get("/api/runs/disk-run/files").json()["files"]

    assert files == {"card_deck.py": "on disk\n"}
    assert client.get("/api/runs/disk-run").json()["files_created"] == ["card_deck.py"]


@requires_git
def test_disk_mode_download_never_reads_outside_the_run_workspace(
    client: TestClient, tmp_path: Path
) -> None:
    """Paths from patches or summary (../outside.py, missing.py) are never served in disk mode."""
    base = tmp_path / "project"
    _write(base / "untouched.py", "same\n")
    workspace = base / ".delegatron-runs" / "disk-dl"
    init_sha = app_module._init_run_workspace(base, workspace, "disk-dl")
    _write(workspace / "card_deck.py", "on disk\n")
    _write(workspace.parent / "outside.py", "SECRET = 1\n")  # next to the workspace
    run_dir = tmp_path / "runs" / "disk-dl"
    summary = {"run_id": "disk-dl", "status": "approved", "request": "Build a deck"}
    _write_json(run_dir / "summary.json", {**summary, "files_changed": ["../outside.py"]})
    _patch(run_dir, 1, {"card_deck.py": "patch\n", "../outside.py": "x\n", "missing.py": "m\n"})
    _write_json(
        run_dir / "meta.json", {"run_workspace": str(workspace), "workspace_init_sha": init_sha}
    )

    download = client.get("/api/runs/disk-dl/download")
    files = client.get("/api/runs/disk-dl/files").json()["files"]

    assert download.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert set(archive.namelist()) == {"card_deck.py", "DELEGATRON_RUN.md"}
    assert archive.read("card_deck.py").decode() == "on disk\n"
    assert "SECRET" not in "".join(archive.read(n).decode() for n in archive.namelist())
    assert files == {"card_deck.py": "on disk\n"}


@requires_git
def test_disk_mode_skips_a_symlink_that_leaves_the_run_workspace(
    client: TestClient, tmp_path: Path
) -> None:
    base = tmp_path / "project"
    _write(base / "untouched.py", "same\n")
    workspace = base / ".delegatron-runs" / "disk-ln"
    init_sha = app_module._init_run_workspace(base, workspace, "disk-ln")
    outside = tmp_path / "outside.py"
    _write(outside, "SECRET = 1\n")
    try:
        (workspace / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is not permitted here")
    _write(workspace / "card_deck.py", "on disk\n")
    run_dir = tmp_path / "runs" / "disk-ln"
    _write_json(run_dir / "summary.json", {"run_id": "disk-ln", "status": "approved"})
    _write_json(
        run_dir / "meta.json", {"run_workspace": str(workspace), "workspace_init_sha": init_sha}
    )

    names = _zip_names(client.get("/api/runs/disk-ln/download"))

    assert names == {"card_deck.py", "DELEGATRON_RUN.md"}
    assert client.get("/api/runs/disk-ln/files").json()["files"] == {"card_deck.py": "on disk\n"}


@requires_git
def test_run_workspace_of_a_repo_with_origin_can_open_a_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run branch lives in a clone that shares history and origin with the base."""
    base = tmp_path / "project"
    base.mkdir()
    _git(base, "init", "-q", "-b", "main")
    _write(base / "app.py", "x = 1\n")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "base")
    _git(base, "remote", "add", "origin", "https://github.com/org/repo.git")
    run_ws = base / ".delegatron-runs" / "r1"
    app_module._init_run_workspace(base, run_ws, "r1")
    workspace = Workspace(repo_path=run_ws, run_id="r1")
    workspace.initialize()
    created: list[dict[str, Any]] = []

    class _FakeCreator:
        def create_pr(self, **kwargs: Any) -> PRResult:
            created.append(kwargs)
            return PRResult(success=True, url="https://github.com/org/repo/pull/1", number=1)

    config = load_config(preset="mock")
    config = replace(config, pipeline=replace(config.pipeline, create_pr=True))
    pipeline = graph.Pipeline(
        config=config, workspace_root=run_ws, run_id="r1", pr_creator=_FakeCreator()
    )
    monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
    state: Any = {"status": "approved", "is_approved": True, "messages": [], "plan": {}}

    result = pipeline._maybe_create_pr(state, workspace, {"status": "approved"}, True)

    assert result["created"] is True
    assert created[0]["branch"] == "delegatron/run-r1"


# --- Restore -----------------------------------------------------------------


def _restorable_run(
    tmp_path: Path, run_id: str = "stalled-run", *, workspace_sha: bool = False
) -> tuple[Path, Path, list[str]]:
    """A stalled run with a git workspace and three committed iterations.

    Checkpoints record each iteration's commit in their ``messages`` (older
    runs) or, with *workspace_sha*, in ``workspace_sha`` only.
    """
    base = tmp_path / "project"
    _write(base / "README.md", "base\n")
    workspace = base / ".delegatron-runs" / run_id
    init_sha = app_module._init_run_workspace(base, workspace, run_id)
    run_dir = tmp_path / "runs" / run_id
    messages: list[dict[str, Any]] = []
    shas: list[str] = []
    for iteration in (1, 2, 3):
        _write(workspace / "game.py", f"v{iteration}\n")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-q", "-m", f"iteration {iteration}")
        shas.append(_git(workspace, "rev-parse", "HEAD"))
        messages.append({"event": "workspace_commit", "sha": shas[-1], "iteration": iteration})
        checkpoint: dict[str, Any] = {
            "iteration": iteration,
            "status": "running",
            "is_approved": iteration == 3,
        }
        if workspace_sha:
            checkpoint.update(workspace_sha=shas[-1], messages=[])
        else:
            checkpoint["messages"] = list(messages)
        _write_json(run_dir / f"state_{iteration:03d}.json", checkpoint)
    _write_json(run_dir / "summary.json", {"run_id": run_id, "status": "stalled", "iterations": 3})
    _write_json(
        run_dir / "meta.json", {"run_workspace": str(workspace), "workspace_init_sha": init_sha}
    )
    return run_dir, workspace, shas


@pytest.mark.parametrize("iteration", ["3", 0, -1, True, None])
def test_restore_rejects_invalid_iterations(
    client: TestClient, tmp_path: Path, iteration: Any
) -> None:
    _write_json(tmp_path / "runs" / "r1" / "summary.json", {"status": "stalled"})

    assert client.post("/api/runs/r1/restore", json={"iteration": iteration}).status_code == 422


def test_restore_of_unknown_run_is_404_without_side_effects(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post("/api/runs/ghost/restore", json={"iteration": 1})

    assert resp.status_code == 404
    assert not (tmp_path / "runs" / "ghost").exists()


@requires_git
def test_restore_resets_the_workspace_and_keeps_the_final_status(
    client: TestClient, tmp_path: Path
) -> None:
    run_dir, workspace, shas = _restorable_run(tmp_path)

    resp = client.post("/api/runs/stalled-run/restore", json={"iteration": 2})

    assert resp.status_code == 200
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["status"] == "stalled"
    assert summary.get("is_approved", False) is False
    assert summary["restored_to_iteration"] == 2
    assert summary["restored_sha"] == shas[1]
    assert summary["pre_restore_sha"] == shas[2]
    assert _git(workspace, "rev-parse", "HEAD") == shas[1]
    assert (workspace / "game.py").read_text(encoding="utf-8") == "v2\n"
    # Checkout may use CRLF (core.autocrlf); the API serves the bytes on disk.
    assert client.get("/api/runs/stalled-run/files").json()["files"]["game.py"].split() == ["v2"]

    assert client.post("/api/runs/stalled-run/restore", json={"iteration": 3}).status_code == 200
    assert _git(workspace, "rev-parse", "HEAD") == shas[2]
    for _ in range(2):
        assert (
            client.post("/api/runs/stalled-run/restore", json={"iteration": 1}).status_code == 200
        )
        assert _git(workspace, "rev-parse", "HEAD") == shas[0]
    assert json.loads((run_dir / "summary.json").read_text())["pre_restore_sha"] == shas[2]


def test_restore_without_a_workspace_is_409_and_changes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / "no-ws"
    summary = {"run_id": "no-ws", "status": "approved"}
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "state_001.json", {"iteration": 1, "status": "running"})

    resp = client.post("/api/runs/no-ws/restore", json={"iteration": 1})

    assert resp.status_code == 409
    assert json.loads((run_dir / "summary.json").read_text()) == summary


def test_restore_of_an_active_run_is_409(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_json(tmp_path / "runs" / "live" / "summary.json", {"status": "running"})
    monkeypatch.setitem(app_module._run_tasks, "live", object())  # type: ignore[arg-type]

    assert client.post("/api/runs/live/restore", json={"iteration": 1}).status_code == 409


def test_summary_corrupted_by_an_old_restore_is_repaired(
    client: TestClient, tmp_path: Path
) -> None:
    run_dir = tmp_path / "runs" / "corrupt"
    _write_json(
        run_dir / "summary.json",
        {"run_id": "corrupt", "status": "running", "is_approved": True, "restored_to_iteration": 3},
    )
    events = [
        {"event": "pipeline_start"},
        {"event": "pipeline_complete", "status": "stalled", "is_approved": False},
    ]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    assert client.get("/api/runs/corrupt").json()["status"] == "stalled"
    assert json.loads((run_dir / "summary.json").read_text())["is_approved"] is False
    assert [r["status"] for r in client.get("/api/runs").json()] == ["stalled"]


def test_corrupted_summary_without_a_final_event_is_repaired_to_error(
    client: TestClient, tmp_path: Path
) -> None:
    """A cancelled (or crashed) run restored by old code never logged pipeline_complete."""
    run_dir = tmp_path / "runs" / "corrupt2"
    _write_json(
        run_dir / "summary.json",
        {
            "run_id": "corrupt2",
            "status": "running",
            "is_approved": True,
            "restored_to_iteration": 2,
        },
    )
    (run_dir / "events.jsonl").write_text(json.dumps({"event": "pipeline_start"}) + "\n")

    assert client.get("/api/runs/corrupt2").json()["status"] == "error"
    repaired = json.loads((run_dir / "summary.json").read_text())
    assert (repaired["status"], repaired["is_approved"]) == ("error", False)
    assert repaired["error"]


def test_a_corrupted_summary_of_a_live_run_is_left_alone(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "live-corrupt"
    summary = {"run_id": "live-corrupt", "status": "running", "restored_to_iteration": 2}
    _write_json(run_dir / "summary.json", summary)
    monkeypatch.setitem(app_module._run_tasks, "live-corrupt", object())  # type: ignore[arg-type]

    assert client.get("/api/runs/live-corrupt").json()["status"] == "running"
    assert json.loads((run_dir / "summary.json").read_text()) == summary


@requires_git
def test_restore_uses_the_checkpoints_workspace_sha(client: TestClient, tmp_path: Path) -> None:
    """Checkpoints of current runs record HEAD in workspace_sha (no workspace_commit messages)."""
    _run_dir, workspace, shas = _restorable_run(tmp_path, "sha-run", workspace_sha=True)

    resp = client.post("/api/runs/sha-run/restore", json={"iteration": 2})

    assert resp.status_code == 200
    assert resp.json()["restored_sha"] == shas[1]
    assert _git(workspace, "rev-parse", "HEAD") == shas[1]
    assert (workspace / "game.py").read_text(encoding="utf-8") == "v2\n"


@requires_git
def test_run_workspace_of_a_non_git_base_reports_why_no_pr_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run workspace is a fresh repo with no origin: the PR is skipped, never attempted."""
    base = tmp_path / "plain"
    _write(base / "app.py", "x = 1\n")
    run_ws = base / ".delegatron-runs" / "r2"
    app_module._init_run_workspace(base, run_ws, "r2")
    workspace = Workspace(repo_path=run_ws, run_id="r2")
    workspace.initialize()
    created: list[dict[str, Any]] = []

    class _FakeCreator:
        def create_pr(self, **kwargs: Any) -> PRResult:
            created.append(kwargs)
            return PRResult(success=True, url="https://example.invalid/pull/1", number=1)

    config = load_config(preset="mock")
    config = replace(config, pipeline=replace(config.pipeline, create_pr=True))
    pipeline = graph.Pipeline(
        config=config, workspace_root=run_ws, run_id="r2", pr_creator=_FakeCreator()
    )
    monkeypatch.setattr(graph, "check_gh_available", lambda: (True, ""))
    state: Any = {"status": "approved", "is_approved": True, "messages": [], "plan": {}}

    result = pipeline._maybe_create_pr(state, workspace, {"status": "approved"}, True)

    assert result["created"] is False
    assert result["skipped_reason"] == "no git remote `origin` configured"
    assert created == []


# --- Delete and cleanup ------------------------------------------------------


def test_delete_removes_the_recorded_run_workspace(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "elsewhere" / ".delegatron-runs" / "gone"
    _write(workspace / "app.py", "x\n")
    run_dir = tmp_path / "runs" / "gone"
    _write_json(run_dir / "summary.json", {"status": "approved"})
    _write_json(run_dir / "meta.json", {"run_workspace": str(workspace)})

    assert client.delete("/api/runs/gone").status_code == 200
    assert not workspace.exists()
    assert not run_dir.exists()


def test_delete_never_removes_a_path_outside_the_run_layout(
    client: TestClient, tmp_path: Path
) -> None:
    precious = tmp_path / "my-project"
    _write(precious / "app.py", "x\n")
    _write_json(tmp_path / "runs" / "evil" / "summary.json", {"status": "approved"})
    _write_json(tmp_path / "runs" / "evil" / "meta.json", {"run_workspace": str(precious)})

    assert client.delete("/api/runs/evil").status_code == 200
    assert (precious / "app.py").exists()


def test_cleanup_removes_the_runs_own_workspace_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(workspace=tmp_path / "server-root", artifacts=tmp_path / "runs")
    custom = tmp_path / "custom" / ".delegatron-runs" / "r1"
    same_id_under_root = tmp_path / "server-root" / ".delegatron-runs" / "r1"
    _write(custom / "a.py", "x\n")
    _write(same_id_under_root / "b.py", "y\n")
    commands: list[list[str]] = []

    def _record(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _record)
    app_module._cleanup_cancelled_run("r1", None, custom)

    assert not custom.exists()
    assert same_id_under_root.exists()
    assert not [c for c in commands if c[:2] == ["git", "branch"]]


def test_cleanup_keeps_the_workspace_of_a_resumable_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled run with a checkpoint resumes from its own files, not a fresh copy."""
    configure(workspace=tmp_path / "server-root", artifacts=tmp_path / "runs")
    workspace = tmp_path / "server-root" / ".delegatron-runs" / "r2"
    _write(workspace / "card_deck.py", "class Deck: ...\n")
    (workspace / ".git").mkdir()
    _write_json(tmp_path / "runs" / "r2" / "state_003.json", {"iteration": 3})
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, "", "")
    )

    app_module._cleanup_cancelled_run("r2", None, workspace)

    assert (workspace / "card_deck.py").exists()
    meta = {"run_workspace": str(workspace)}
    assert app_module._resumable_workspace("r2", meta) == workspace


def test_cleanup_removes_the_workspace_of_a_run_without_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing can resume such a run, so nothing keeps its workspace."""
    configure(workspace=tmp_path / "server-root", artifacts=tmp_path / "runs")
    workspace = tmp_path / "server-root" / ".delegatron-runs" / "r3"
    _write(workspace / "card_deck.py", "class Deck: ...\n")
    _write_json(tmp_path / "runs" / "r3" / "summary.json", {"status": "error"})
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, "", "")
    )

    app_module._cleanup_cancelled_run("r3", None, workspace)

    assert not workspace.exists()


# --- Pipeline finalization in _execute_run ------------------------------------


class _FakePipeline:
    """Stands in for graph.Pipeline; returns a canned final state."""

    final_state: ClassVar[dict[str, Any]] = {}
    writes_summary: ClassVar[bool] = False
    configs: ClassVar[list[Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        _FakePipeline.configs.append(kwargs["config"])

    async def execute(self, request: str, **_kwargs: Any) -> dict[str, Any]:
        if self.writes_summary:
            run_dir = self._kwargs["artifacts_dir"] / self._kwargs["run_id"]
            _write_json(run_dir / "summary.json", {"status": "approved", "from": "pipeline"})
        return dict(self.final_state)


@pytest.fixture
def alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Run _execute_run against fakes; returns the alert webhooks it sends."""
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(_FakePipeline, "configs", [])

    async def _alert(run_id: str, status: str, request: str) -> None:
        sent.append((run_id, status))

    def _init(base: Path, target: Path, run_id: str) -> str:
        target.mkdir(parents=True)
        return "init-sha"

    real_sleep = asyncio.sleep

    async def _no_linger(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("delegatron.orchestrator.graph.Pipeline", _FakePipeline)
    monkeypatch.setattr(app_module, "_send_alert_webhook", _alert)
    monkeypatch.setattr(app_module, "_init_run_workspace", _init)
    monkeypatch.setattr(asyncio, "sleep", _no_linger)
    return sent


async def test_early_pipeline_failure_gets_a_summary_and_an_alert(
    tmp_path: Path, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_FakePipeline, "writes_summary", False)
    monkeypatch.setattr(
        _FakePipeline, "final_state", {"status": "error", "error": "Architect failed: bad JSON"}
    )
    monkeypatch.setattr(app_module, "_run_tasks", {})
    req = app_module.RunRequest(request="Build a deck", preset="mock")

    # Registered and unregistered as _start_run_internal's task and done-callback do.
    task = asyncio.create_task(app_module._execute_run("early", req))
    app_module._run_tasks["early"] = task
    task.add_done_callback(lambda _t: app_module._run_tasks.pop("early", None))
    await task
    await asyncio.sleep(0)  # let the done-callback run

    assert "early" not in app_module._run_tasks
    summary = json.loads((tmp_path / "runs" / "early" / "summary.json").read_text())
    assert summary["status"] == "error"
    assert summary["error"].startswith("Architect failed:")
    assert summary["request"] == "Build a deck"
    assert alerts == [("early", "error")]
    app_module._invalidate_runs_cache()
    [listed] = [r for r in app_module._get_cached_runs() if r["run_id"] == "early"]
    assert listed["status"] == "error"
    assert listed["request"] == "Build a deck"


async def test_pipeline_summary_is_kept_and_approved_runs_send_no_alert(
    tmp_path: Path, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_FakePipeline, "writes_summary", True)
    monkeypatch.setattr(_FakePipeline, "final_state", {"status": "approved"})

    await app_module._execute_run("ok", app_module.RunRequest(request="x", preset="mock"))

    summary = json.loads((tmp_path / "runs" / "ok" / "summary.json").read_text())
    assert summary["from"] == "pipeline"
    assert alerts == []


async def test_run_workspace_is_recorded_in_meta(
    tmp_path: Path, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_FakePipeline, "final_state", {"status": "approved"})
    base = tmp_path / "project"
    base.mkdir()
    req = app_module.RunRequest(request="x", preset="mock", workspace=str(base))

    await app_module._execute_run("rec", req)

    meta = json.loads((tmp_path / "runs" / "rec" / "meta.json").read_text())
    expected = (base / ".delegatron-runs" / "rec").resolve()
    assert Path(meta["run_workspace"]) == expected
    assert meta["workspace_init_sha"] == "init-sha"


async def test_request_constraints_are_merged_with_the_config_file(
    tmp_path: Path, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_FakePipeline, "final_state", {"status": "approved"})
    (tmp_path / "delegatron.toml").write_text('[constraints]\nno_modify = [".env"]\n')
    req = app_module.RunRequest(
        request="x",
        preset="mock",
        config_path="delegatron.toml",
        constraints={"forbidden_patterns": ["eval("]},
    )

    await app_module._execute_run("merged", req)

    constraints = _FakePipeline.configs[-1].pipeline.constraints
    assert constraints["no_modify"] == [".env"]
    assert "eval(" in constraints["forbidden_patterns"].values()


async def test_workspace_init_failure_is_an_error_summary(
    tmp_path: Path, alerts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(base: Path, target: Path, run_id: str) -> str:
        raise WorkspaceInitError("git is not installed or not on PATH")

    monkeypatch.setattr(app_module, "_init_run_workspace", _fail)

    await app_module._execute_run("nogit", app_module.RunRequest(request="x", preset="mock"))

    summary = json.loads((tmp_path / "runs" / "nogit" / "summary.json").read_text())
    assert summary["status"] == "error"
    assert "git is not installed" in summary["error"]


# --- Configuration from the environment (Docker image) ------------------------


def test_configure_from_env_applies_workspace_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "_configured", False)
    monkeypatch.setenv("DELEGATRON_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DELEGATRON_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    app_module._configure_from_env()

    assert app_module._workspace_root == (tmp_path / "ws").resolve()
    assert app_module._artifacts_base == tmp_path / "artifacts"

    monkeypatch.setenv("DELEGATRON_WORKSPACE", str(tmp_path / "other"))
    app_module._configure_from_env()
    assert app_module._workspace_root == (tmp_path / "ws").resolve()


def test_configure_from_env_without_the_variable_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(workspace=tmp_path, artifacts=tmp_path / "runs")
    monkeypatch.setattr(app_module, "_configured", False)
    monkeypatch.delenv("DELEGATRON_WORKSPACE", raising=False)

    app_module._configure_from_env()

    assert app_module._workspace_root == tmp_path
    assert app_module._artifacts_base == tmp_path / "runs"


def test_configure_defaults_artifacts_to_the_env_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DELEGATRON_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    configure(workspace=tmp_path)

    assert app_module._artifacts_base == tmp_path / "artifacts"


def test_review_sends_files_the_reviewer_can_trim_within_its_window(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /api/review builds its input like the CLI: ranked files as related_files
    # (the agent's context guard can trim them), sized to the reviewer's window.
    import delegatron.factories.agent_factory as agent_factory

    project = tmp_path / "plain"
    _write(project / "app.py", "def main():\n    return 1\n")
    _write(project / "big.py", "x = 1\n" * 4000)
    sent: list[dict[str, Any]] = []

    class _Reviewer:
        async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
            sent.append(input_data)
            meta = {"model": "mock", "input_tokens": 10, "output_tokens": 5}
            return {"summary": "ok", "acceptance_met": True, "issues": [], "_meta": meta}

    window = 2000  # tokens: half of it, in bytes, is the file budget
    real_load = app_module.load_config

    def load(*args: Any, **kwargs: Any) -> Any:
        config = real_load(*args, **kwargs)
        return replace(config, reviewer=replace(config.reviewer, context_window=window))

    monkeypatch.setattr(app_module, "load_config", load)
    monkeypatch.setattr(agent_factory, "create_reviewer", lambda config: _Reviewer())

    resp = client.post(
        "/api/review",
        json={"preset": "mock", "workspace": str(project), "focus": "performance"},
    )

    assert resp.status_code == 200, resp.text
    (data,) = sent
    files = data["related_files"]
    assert "app.py" in files
    assert sum(len(text.encode("utf-8")) for text in files.values()) <= window * 3.5 // 2
    assert "--- File:" not in data["diff"]
    assert "caching" in data["request"]  # the same focus text as the CLI
    assert resp.json()["files_reviewed"] == len(files)
