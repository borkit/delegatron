"""Delegatron's internal dirs are kept out of git without touching ``.gitignore``.

Regression: run artifacts (``runs/*.json``), Docker scratch (``_sandbox/``),
worktrees, server run workspaces, review output and the LocalSandbox venv used
to land in run-branch commits.  ``Workspace.initialize()`` now writes
root-anchored patterns for Delegatron's internal dirs (``internal_dirs_for``:
``runs/`` and ``reviews/`` only when they hold Delegatron's layout) to the
repo's ``info/exclude`` — no ``.gitignore`` edit and no commit, so PR diffs
stay clean (#121; replaces the earlier committed ``.gitignore`` block).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from delegatron.utils.paths import ALWAYS_INTERNAL_DIRS, INTERNAL_DIRS
from delegatron.workspace import Workspace


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a tmp git repo with one initial commit."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "init")
    return tmp_path


def _exclude_lines(repo: Path) -> list[str]:
    path = repo / ".git" / "info" / "exclude"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _exclude_lines_bytes(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8", errors="replace").splitlines()


def _delegatron_artifacts(root: Path) -> None:
    """The run and review dirs a pipeline writes before it initializes the workspace."""
    for rel in ("runs/abc/events.jsonl", "reviews/review-x/summary.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("{}\n", encoding="utf-8")


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


class TestInfoExclude:
    def test_writes_anchored_patterns_without_commit(self, git_repo: Path) -> None:
        before = _commit_count(git_repo)

        Workspace(repo_path=git_repo, run_id="ex-1").initialize()

        assert sorted(_exclude_lines(git_repo)) == sorted(f"/{n}/" for n in ALWAYS_INTERNAL_DIRS)
        assert not (git_repo / ".gitignore").exists()
        assert _commit_count(git_repo) == before

    def test_delegatron_runs_and_reviews_dirs_are_excluded(self, git_repo: Path) -> None:
        _delegatron_artifacts(git_repo)

        Workspace(repo_path=git_repo, run_id="ex-1b").initialize()

        assert sorted(_exclude_lines(git_repo)) == sorted(f"/{n}/" for n in INTERNAL_DIRS)

    def test_user_reviews_package_is_not_excluded(self, git_repo: Path) -> None:
        (git_repo / "reviews").mkdir()
        (git_repo / "reviews" / "models.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", "reviews")
        _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "app")

        ws = Workspace(repo_path=git_repo, run_id="ex-1c")
        ws.initialize()
        ws.apply_files({"reviews/views.py": "y = 2\n"})
        ws.commit("views")

        assert "/reviews/" not in _exclude_lines(git_repo)
        assert "reviews/views.py" in _git(git_repo, "ls-files", "reviews/")

    def test_second_initialize_adds_no_duplicates(self, git_repo: Path) -> None:
        Workspace(repo_path=git_repo, run_id="ex-2").initialize()
        Workspace(repo_path=git_repo, run_id="ex-2").initialize()

        lines = _exclude_lines(git_repo)
        assert len(lines) == len(set(lines)) == len(ALWAYS_INTERNAL_DIRS)

    def test_existing_gitignore_is_left_untouched(self, git_repo: Path) -> None:
        (git_repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "gi")

        Workspace(repo_path=git_repo, run_id="ex-3").initialize()

        assert (git_repo / ".gitignore").read_text(encoding="utf-8") == "__pycache__/\n"

    def test_non_utf8_gitignore_and_exclude_are_kept_byte_for_byte(self, git_repo: Path) -> None:
        latin1 = b"# caf\xe9\n*.log\n"
        (git_repo / ".gitignore").write_bytes(latin1)
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "gi")
        exclude = git_repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_bytes(latin1)

        Workspace(repo_path=git_repo, run_id="ex-3b").initialize()

        assert (git_repo / ".gitignore").read_bytes() == latin1
        assert exclude.read_bytes().startswith(latin1)
        assert "/_sandbox/" in _exclude_lines_bytes(exclude)

    def test_subdirectory_workspace_anchors_patterns_to_it(self, git_repo: Path) -> None:
        sub = git_repo / "services" / "billing"
        sub.mkdir(parents=True)
        (sub / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "svc")

        Workspace(repo_path=sub, run_id="ex-4").initialize()

        assert "/services/billing/_sandbox/" in _exclude_lines(git_repo)


class TestInternalDirsNeverCommitted:
    def test_internal_dirs_stay_out_of_commits(self, git_repo: Path) -> None:
        _delegatron_artifacts(git_repo)
        ws = Workspace(repo_path=git_repo, run_id="ex-5")
        ws.initialize()
        for rel in (
            ".delegatron-venv/bin/python",
            "reviews/review-x/summary.json",
            "runs/abc/state_001.json",
        ):
            (git_repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (git_repo / rel).write_text("{}", encoding="utf-8")
        nested = git_repo / ".delegatron-runs" / "abc"
        nested.mkdir(parents=True)
        _git(nested, "init", "-q")

        assert ws.commit("x") is None
        assert _git(git_repo, "status", "--porcelain").strip() == ""
        assert _git(git_repo, "ls-files").strip() == ""

    def test_nested_dirs_with_internal_names_stay_visible(self, git_repo: Path) -> None:
        ws = Workspace(repo_path=git_repo, run_id="ex-6")
        ws.initialize()

        ws.apply_files({"src/runs/a.py": "a = 1\n", "src/reviews/b.py": "b = 2\n"})
        ws.commit("nested")

        assert set(_git(git_repo, "ls-files").split()) == {"src/runs/a.py", "src/reviews/b.py"}


class TestCollisionSafety:
    def test_tracked_runs_dir_is_not_excluded(
        self, git_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (git_repo / "runs").mkdir()
        (git_repo / "runs" / "keep.txt").write_text("important\n", encoding="utf-8")
        _git(git_repo, "add", "runs/")
        _git(git_repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "add runs")
        _delegatron_artifacts(git_repo)  # Delegatron's runs land in the same dir
        logger = logging.getLogger("delegatron.workspace")
        logger.addHandler(caplog.handler)
        try:
            Workspace(repo_path=git_repo, run_id="collision-1").initialize()
        finally:
            logger.removeHandler(caplog.handler)

        lines = _exclude_lines(git_repo)
        assert "/runs/" not in lines
        assert "/_sandbox/" in lines
        assert "runs/keep.txt" in _git(git_repo, "ls-files", "runs/")
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("'runs'" in message for message in warnings)
        assert not any("DELEGATRON_ARTIFACTS_DIR" in message for message in warnings)
