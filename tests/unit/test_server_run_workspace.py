"""Tests for server run workspaces: creation, lookup, removal and run files."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from delegatron.integrations.git_pr import check_git_remote
from delegatron.server import run_workspace as rw
from delegatron.workspace import Workspace, WorkspaceInitError

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

ORIGIN = "https://github.com/org/repo.git"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _make_repo(path: Path, files: dict[str, str], *, origin: str | None = None) -> str:
    """A committed repo on ``main``; returns HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        _write(path / rel, text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return _git(path, "rev-parse", "HEAD")


def _tracked(repo: Path) -> set[str]:
    return set(_git(repo, "ls-files").splitlines())


# --- init_run_workspace ----------------------------------------------------


@requires_git
def test_git_base_is_cloned_with_its_origin_and_history(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base_head = _make_repo(base, {"app.py": "v1\n"}, origin=ORIGIN)
    _write(base / "app.py", "v2 uncommitted\n")
    _write(base / "notes.md", "untracked\n")
    target = base / ".delegatron-runs" / "r1"

    sha = rw.init_run_workspace(base, target, "r1")

    assert check_git_remote(target) == (True, "")
    assert _git(target, "remote", "get-url", "origin") == ORIGIN
    assert _git(target, "merge-base", "HEAD", base_head) == base_head
    assert sha == _git(target, "rev-parse", "HEAD")
    assert _git(target, "log", "-1", "--format=%s") == "init: workspace snapshot for run r1"
    assert (target / "app.py").read_text(encoding="utf-8") == "v2 uncommitted\n"
    assert (target / "notes.md").exists()
    assert _git(target, "status", "--porcelain") == ""


@requires_git
def test_clean_git_base_adds_no_snapshot_commit(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base_head = _make_repo(base, {"app.py": "v1\n"}, origin=ORIGIN)

    sha = rw.init_run_workspace(base, tmp_path / "run", "r1")

    assert sha == base_head


@requires_git
def test_git_base_without_origin_gets_no_remote(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _make_repo(base, {"app.py": "v1\n"})
    target = tmp_path / "run"

    rw.init_run_workspace(base, target, "r1")

    assert _git(target, "remote") == ""
    assert check_git_remote(target)[0] is False


@requires_git
@pytest.mark.parametrize("files", [{"app.py": "x = 1\n"}, {}])
def test_non_git_base_becomes_a_repo_on_main(tmp_path: Path, files: dict[str, str]) -> None:
    base = tmp_path / "plain"
    base.mkdir()
    for rel, text in files.items():
        _write(base / rel, text)
    target = tmp_path / "run"

    sha = rw.init_run_workspace(base, target, "r1")

    assert _git(target, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(target, "rev-parse", "HEAD") == sha
    assert _tracked(target) == set(files)


@requires_git
def test_subdirectory_of_a_repo_is_copied_not_cloned(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    _make_repo(repo, {"services/billing/app.py": "x\n", "other.py": "y\n"}, origin=ORIGIN)
    target = tmp_path / "run"

    rw.init_run_workspace(repo / "services" / "billing", target, "r1")

    assert _tracked(target) == {"app.py"}
    assert _git(target, "remote") == ""


@requires_git
def test_internal_dirs_and_server_stores_are_not_copied(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    for rel in (
        "app.py",
        "reviews/review-1/summary.json",
        "runs/abc/state_001.json",
        ".delegatron-venv/bin/python",
        ".delegatron-runs/old/app.py",
        "server-data/delegatron.db",
        "src/runs/a.py",
        "src/reviews/b.py",
        "pkg/server-data/keep.txt",
        "node_modules/left/index.js",
    ):
        _write(base / rel)
    target = base / ".delegatron-runs" / "new"

    rw.init_run_workspace(base, target, "new", skip_paths=[base / "server-data"])

    assert _tracked(target) == {
        "app.py",
        "src/runs/a.py",
        "src/reviews/b.py",
        "pkg/server-data/keep.txt",
    }


@requires_git
def test_only_the_roots_logs_dir_is_skipped(tmp_path: Path) -> None:
    # A project's nested logs/runs/reviews packages are code, not tooling output.
    base = tmp_path / "base"
    base.mkdir()
    for rel in ("app/logs/handlers.py", "app/runs/x.py", "app/reviews/models.py", "logs/srv.log"):
        _write(base / rel)
    target = base / ".delegatron-runs" / "new"

    rw.init_run_workspace(base, target, "new")

    assert _tracked(target) == {"app/logs/handlers.py", "app/runs/x.py", "app/reviews/models.py"}


def _excluded(repo: Path) -> list[str]:
    return (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()


@requires_git
def test_internal_dirs_are_excluded_from_later_commits(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _make_repo(base, {"app.py": "x\n", "runs/keep.txt": "tracked\n"})
    target = tmp_path / "run"
    rw.init_run_workspace(base, target, "r1")

    exclude = _excluded(target)
    for name in ("_sandbox", ".delegatron-runs", ".delegatron-worktrees", ".delegatron-venv"):
        assert f"/{name}/" in exclude
    # A directory the project tracks files under is left visible, and a
    # ``reviews/`` the base does not hold as Delegatron's review store is user code.
    assert "/runs/" not in exclude
    assert "/reviews/" not in exclude
    _write(target / "_sandbox" / "workspace" / "x.py")
    _write(target / ".delegatron-venv" / "bin" / "python")
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == ""


@requires_git
def test_delegatron_runs_and_reviews_of_the_base_are_excluded(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _make_repo(base, {"app.py": "x\n"})
    _write(base / "runs" / "abc" / "events.jsonl", "{}\n")
    _write(base / "reviews" / "review-1" / "summary.json", "{}")
    target = tmp_path / "run"

    rw.init_run_workspace(base, target, "r1")

    exclude = _excluded(target)
    assert "/runs/" in exclude
    assert "/reviews/" in exclude
    _write(target / "runs" / "def" / "summary.json", "{}")
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == ""


@requires_git
def test_the_issue_81_base_commits_only_the_project(tmp_path: Path) -> None:
    base = tmp_path / "base"
    for rel in (
        "reviews/review-0a891ba6/summary.json",
        ".delegatron-worktrees/r1/t1/a.py",
        "runs/x/state_001.json",
        "logs/delegatron.log",
        "_sandbox/workspace/b.py",
        ".delegatron-runs/old/c.py",
        "card_deck.py",
    ):
        _write(base / rel)
    target = base / ".delegatron-runs" / "r1"

    rw.init_run_workspace(base, target, "r1")

    assert _tracked(target) == {"card_deck.py"}


@requires_git
def test_user_runs_and_reviews_packages_are_committed(tmp_path: Path) -> None:
    # D4: a root ``reviews/`` (a Django app) or ``runs/`` without Delegatron's
    # layout is user code, in the init commit and in every later run commit.
    base = tmp_path / "plain"
    for rel in ("app.py", "reviews/README.md", "reviews/models.py", "runs/job.py"):
        _write(base / rel)
    target = base / ".delegatron-runs" / "r1"

    init_sha = rw.init_run_workspace(base, target, "r1")

    assert _tracked(target) == {"app.py", "reviews/README.md", "reviews/models.py", "runs/job.py"}
    assert "/reviews/" not in _excluded(target)
    assert "/runs/" not in _excluded(target)
    workspace = Workspace(repo_path=target, run_id="r1")
    workspace.initialize()
    workspace.apply_files({"reviews/views.py": "v = 1\n", "runs/queue.py": "q = []\n"})
    assert workspace.commit("iteration 1")
    assert {"reviews/views.py", "runs/queue.py"} <= _tracked(target)
    assert {"reviews/views.py", "runs/queue.py"} <= set(workspace.get_file_tree())

    run_dir = tmp_path / "artifacts" / "r1"
    meta = {"run_workspace": str(target), "workspace_init_sha": init_sha}
    assert rw.run_file_paths(run_dir, meta) == ["reviews/views.py", "runs/queue.py"]


@requires_git
def test_workspace_seeded_from_a_run_continues_its_branch(tmp_path: Path) -> None:
    """A resumed run's seed is a run workspace; its branch carries on under the new id."""
    seed = tmp_path / "old-run"
    _make_repo(seed, {"app.py": "v1\n"}, origin=ORIGIN)
    _git(seed, "checkout", "-q", "-b", "delegatron/run-old")
    _write(seed / "feature.py", "done\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "iteration 1")
    target = tmp_path / "new-run"

    rw.init_run_workspace(seed, target, "new")

    assert _git(target, "symbolic-ref", "--short", "HEAD") == "delegatron/run-new"
    workspace = Workspace(repo_path=target, run_id="new")
    workspace.initialize()
    assert workspace.branch == "delegatron/run-new"
    assert (target / "feature.py").exists()


@requires_git
def test_failed_init_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    _write(base / "app.py")
    target = tmp_path / "run"

    def _fail_copy(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rw.shutil, "copytree", _fail_copy)
    with pytest.raises(WorkspaceInitError, match="disk full"):
        rw.init_run_workspace(base, target, "r1")

    assert not target.exists()
    assert not target.with_name("run.tmp").exists()


@requires_git
def test_stale_staging_from_an_interrupted_init_is_replaced(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _write(base / "app.py")
    target = tmp_path / "run"
    _write(target.with_name("run.tmp") / "half-copied.py")

    rw.init_run_workspace(base, target, "r1")

    assert _tracked(target) == {"app.py"}
    assert not target.with_name("run.tmp").exists()


def test_missing_git_is_reported_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rw.shutil, "which", lambda _name: None)
    target = tmp_path / "run"

    with pytest.raises(WorkspaceInitError, match="git is not installed"):
        rw.init_run_workspace(tmp_path, target, "r1")

    assert not target.exists()


# --- lookup and removal ----------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ("/srv/repo/.delegatron-runs/r1", True),
        ("/srv/repo/.delegatron-runs/other", False),
        ("/srv/repo", False),
        ("/srv/repo/runs/r1", False),
        ("", False),
        (None, False),
    ],
)
def test_recorded_run_workspace_accepts_only_the_run_layout(
    recorded: str | None, expected: bool
) -> None:
    result = rw.recorded_run_workspace("r1", {"run_workspace": recorded})
    assert (result is not None) is expected


@requires_git
def test_remove_tree_deletes_a_git_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo, {"a.txt": "x\n"})

    rw.remove_tree(repo)

    assert not repo.exists()


@requires_git
def test_remove_tree_works_without_rmtree_onexc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python 3.11 (the declared minimum) has no ``onexc``; ``onerror`` is used.
    import warnings

    repo = tmp_path / "repo"
    _make_repo(repo, {"a.txt": "x\n"})
    monkeypatch.setattr(rw, "_RMTREE_HAS_ONEXC", False)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # onerror is deprecated on 3.12+
        rw.remove_tree(repo)

    assert not repo.exists()


def test_iteration_sha_prefers_recorded_head_then_last_commit_up_to_iteration() -> None:
    messages = [
        {"event": "workspace_commit", "sha": "aaa", "iteration": 1},
        {"event": "other", "sha": "zzz", "iteration": 2},
        {"event": "workspace_commit", "sha": "ccc", "iteration": 3},
        {"event": "workspace_commit", "sha": "ddd", "iteration": 4},
    ]
    assert rw.iteration_sha({"messages": messages}, 3) == "ccc"
    assert rw.iteration_sha({"messages": messages}, 2) == "aaa"
    assert rw.iteration_sha({"messages": messages}, 0) == ""
    assert rw.iteration_sha({"workspace_sha": "eee", "messages": messages}, 3) == "eee"


# --- files a run produced --------------------------------------------------


def _patch(run_dir: Path, iteration: int, files: dict[str, str], **extra: Any) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {"files": files, "diff_unified": "", "rationale": "", **extra}
    (run_dir / f"patch_set_{iteration:03d}.json").write_text(json.dumps(data), encoding="utf-8")


def _two_task_run(run_dir: Path) -> None:
    _patch(run_dir, 1, {"card_deck.py": "deck v1\n", "tests/test_card_deck.py": "t v1\n"})
    _patch(run_dir, 3, {"tests/test_card_deck.py": "t v3\n"})
    _patch(run_dir, 4, {"simulate_deal.py": "deal\n", "tests/test_simulate_deal.py": "td\n"})


def test_artifact_fallback_unions_every_patch_set_later_wins(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    _two_task_run(run_dir)

    files = rw.collect_run_files(run_dir, {})

    assert files == {
        "card_deck.py": "deck v1\n",
        "simulate_deal.py": "deal\n",
        "tests/test_card_deck.py": "t v3\n",
        "tests/test_simulate_deal.py": "td\n",
    }
    assert rw.run_file_paths(run_dir, {}) == list(files)


def test_patch_sets_are_ordered_numerically(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    _patch(run_dir, 999, {"a.py": "old\n"})
    _patch(run_dir, 1000, {"a.py": "new\n"})

    assert rw.collect_run_files(run_dir, {}) == {"a.py": "new\n"}


def test_fallback_honours_file_history_deletions_and_unsafe_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    _patch(run_dir, 1, {"a.py": "a\n", "never_applied.py": "x\n", "../outside.py": "evil\n"})
    _patch(run_dir, 2, {"b.py": "b\n", "gone.py": "g\n"})
    _patch(run_dir, 3, {}, delete_files=["gone.py"])
    history = {p: {"task_id": "t1", "iteration": 1} for p in ("a.py", "b.py", "gone.py")}
    (run_dir / "state_003.json").write_text(json.dumps({"_file_history": history}))

    assert rw.collect_run_files(run_dir, {}) == {"a.py": "a\n", "b.py": "b\n"}


@requires_git
def test_workspace_mode_reads_every_changed_file_from_disk(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _make_repo(base, {"untouched.py": "same\n", "card_deck.py": "base\n", "old.py": "o\n"})
    workspace = base / ".delegatron-runs" / "r1"
    init_sha = rw.init_run_workspace(base, workspace, "r1")
    _write(workspace / "card_deck.py", "disk version\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "iteration 1")
    _write(workspace / "simulate_deal.py", "uncommitted new file\n")
    (workspace / "old.py").unlink()
    _write(workspace / "_sandbox" / "scratch.py")
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    run_dir = tmp_path / "runs" / "r1"
    _patch(run_dir, 1, {"card_deck.py": "patch version\n"})
    meta = {"run_workspace": str(workspace), "workspace_init_sha": init_sha}

    files = rw.collect_run_files(run_dir, meta)

    assert files == {
        "card_deck.py": "disk version\n",
        "simulate_deal.py": "uncommitted new file\n",
    }
    assert rw.run_file_paths(run_dir, meta) == ["blob.bin", "card_deck.py", "simulate_deal.py"]


@requires_git
def test_workspace_mode_leaves_out_untracked_byproducts(tmp_path: Path) -> None:
    # A local sandbox installs and tests in the run workspace itself: bytecode,
    # installed packages and egg-info are not the run's files, and with a
    # checkpoint only the untracked files the run wrote count.
    base = tmp_path / "base"
    _make_repo(base, {"setup.py": "base\n"})
    workspace = base / ".delegatron-runs" / "r1"
    init_sha = rw.init_run_workspace(base, workspace, "r1")
    _write(workspace / "card_deck.py", "deck\n")
    _write(workspace / "__pycache__" / "card_deck.cpython-312.pyc", "bytecode\n")
    _write(workspace / "node_modules" / "left-pad" / "index.js", "js\n")
    _write(workspace / "mypkg.egg-info" / "PKG-INFO", "info\n")
    _write(workspace / "stray.txt", "tool output\n")
    run_dir = tmp_path / "runs" / "r1"
    meta = {"run_workspace": str(workspace), "workspace_init_sha": init_sha}

    without_checkpoint = rw.run_file_paths(run_dir, meta)
    run_dir.mkdir(parents=True)
    history = {"card_deck.py": {"iteration": 1}}
    (run_dir / "state_001.json").write_text(json.dumps({"_file_history": history}))

    assert without_checkpoint == ["card_deck.py", "stray.txt"]
    assert rw.collect_run_files(run_dir, meta) == {"card_deck.py": "deck\n"}


def test_artifact_fallback_keeps_user_runs_and_reviews_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "artifacts" / "r1"
    _patch(
        run_dir,
        1,
        {
            "reviews/models.py": "m\n",
            "runs/job.py": "j\n",
            "_sandbox/scratch.py": "s\n",
            ".delegatron-runs/x/a.py": "a\n",
        },
    )

    assert rw.collect_run_files(run_dir, {}) == {"reviews/models.py": "m\n", "runs/job.py": "j\n"}


@requires_git
def test_workspace_mode_leaves_out_internal_dirs_by_layout(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _make_repo(base, {"app.py": "x\n"})
    workspace = base / ".delegatron-runs" / "r1"
    init_sha = rw.init_run_workspace(base, workspace, "r1")
    _write(workspace / "runs" / "jobs.py", "user code\n")
    _write(workspace / "_sandbox" / "scratch.py")
    run_dir = tmp_path / "artifacts" / "r1"
    meta = {"run_workspace": str(workspace), "workspace_init_sha": init_sha}

    assert rw.run_file_paths(run_dir, meta) == ["runs/jobs.py"]


def test_workspace_mode_falls_back_when_the_workspace_is_gone(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    _patch(run_dir, 1, {"a.py": "from patch\n"})
    meta = {
        "run_workspace": str(tmp_path / ".delegatron-runs" / "r1"),
        "workspace_init_sha": "0" * 40,
    }

    assert rw.collect_run_files(run_dir, meta) == {"a.py": "from patch\n"}


def test_materialize_diff_builds_new_files() -> None:
    diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"
    assert rw.materialize_diff(diff) == {"new.py": "a = 1\nb = 2\n"}
    assert rw.materialize_diff("") == {}
