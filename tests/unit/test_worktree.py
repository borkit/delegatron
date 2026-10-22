"""Tests for git worktree isolation module."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from delegatron.worktree import WorktreeError, WorktreeInfo, WorktreeManager, worktree_key


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with an initial commit."""
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    _git("init")
    _git("config", "user.email", "test@test.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")

    (tmp_path / "README.md").write_text("# Test\n")
    _git("add", ".")
    _git("commit", "-m", "init")

    return tmp_path


@pytest.fixture
def manager(git_repo: Path) -> WorktreeManager:
    """Return a WorktreeManager bound to the temp repo."""
    return WorktreeManager(repo_path=git_repo, run_id="run-001")


# ------------------------------------------------------------------
# create / cleanup
# ------------------------------------------------------------------


class TestCreate:
    def test_creates_worktree_directory(self, manager: WorktreeManager) -> None:
        path = manager.create("task-a")
        assert path.is_dir()
        assert (path / "README.md").exists()

    def test_worktree_path_matches_convention(self, manager: WorktreeManager) -> None:
        path = manager.create("task-b")
        assert ".delegatron-worktrees" in str(path)
        assert "run-001" in str(path)
        assert "task-b" in str(path)

    def test_duplicate_raises(self, manager: WorktreeManager) -> None:
        manager.create("dup")
        with pytest.raises(WorktreeError, match="already exists"):
            manager.create("dup")


class TestCleanup:
    def test_cleanup_removes_worktree(self, manager: WorktreeManager) -> None:
        path = manager.create("task-c")
        assert path.is_dir()

        manager.cleanup("task-c")
        assert not path.exists()

    def test_cleanup_idempotent(self, manager: WorktreeManager) -> None:
        """Cleaning up a non-existent worktree should not raise."""
        manager.cleanup("nonexistent")


# ------------------------------------------------------------------
# list_worktrees
# ------------------------------------------------------------------


class TestListWorktrees:
    def test_empty_when_none_created(self, manager: WorktreeManager) -> None:
        assert manager.list_worktrees() == []

    def test_lists_created_worktrees(self, manager: WorktreeManager) -> None:
        manager.create("t1")
        manager.create("t2")
        infos = manager.list_worktrees()

        assert len(infos) == 2
        task_ids = {i.task_id for i in infos}
        assert task_ids == {"t1", "t2"}

        for info in infos:
            assert isinstance(info, WorktreeInfo)
            assert info.exists is True
            assert "delegatron/wt-" in info.branch

    def test_list_after_cleanup(self, manager: WorktreeManager) -> None:
        manager.create("x")
        manager.cleanup("x")
        assert manager.list_worktrees() == []


# ------------------------------------------------------------------
# cleanup_all
# ------------------------------------------------------------------


class TestCleanupAll:
    def test_removes_all_worktrees(self, manager: WorktreeManager) -> None:
        manager.create("a")
        manager.create("b")
        manager.create("c")

        assert len(manager.list_worktrees()) == 3

        manager.cleanup_all()
        assert manager.list_worktrees() == []

    def test_cleanup_all_when_empty(self, manager: WorktreeManager) -> None:
        """Should not raise when there are no worktrees."""
        manager.cleanup_all()


# ------------------------------------------------------------------
# merge_into
# ------------------------------------------------------------------


class TestMergeInto:
    def test_merge_success(self, manager: WorktreeManager, git_repo: Path) -> None:
        # Figure out current branch name
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        target = result.stdout.strip()

        # Create worktree and add a file in it
        wt_path = manager.create("merge-me")
        (wt_path / "new_file.txt").write_text("hello\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "add new_file"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        )

        success = manager.merge_into("merge-me", target)
        assert success is True
        assert (git_repo / "new_file.txt").exists()

    def test_merge_conflict_returns_false(self, manager: WorktreeManager, git_repo: Path) -> None:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        target = result.stdout.strip()

        wt_path = manager.create("conflict-task")

        # Modify README in worktree
        (wt_path / "README.md").write_text("worktree version\n")
        subprocess.run(["git", "add", "."], cwd=wt_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "wt change"],
            cwd=wt_path,
            capture_output=True,
            check=True,
        )

        # Modify same file on target branch (in main repo)
        (git_repo / "README.md").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "main change"],
            cwd=git_repo,
            capture_output=True,
            check=True,
        )

        success = manager.merge_into("conflict-task", target)
        assert success is False


# ------------------------------------------------------------------
# context manager
# ------------------------------------------------------------------


class TestContextManager:
    def test_creates_and_cleans_up(self, manager: WorktreeManager) -> None:
        with manager.worktree("ctx-task") as path:
            assert path.is_dir()
            assert (path / "README.md").exists()

        # After exiting, worktree should be gone
        assert not path.exists()

    def test_cleans_up_on_exception(self, manager: WorktreeManager) -> None:
        with pytest.raises(ValueError, match="boom"), manager.worktree("err-task") as path:
            recorded_path = path
            raise ValueError("boom")

        assert not recorded_path.exists()


# ------------------------------------------------------------------
# worktree ids (#100)
# ------------------------------------------------------------------


class TestWorktreeKey:
    LONG = "implement-the-card-deck-and-the-dealer"  # 30-character prefix shared below

    def test_shared_prefix_gives_distinct_ids(self) -> None:
        assert worktree_key(self.LONG + "-a") != worktree_key(self.LONG + "-b")

    def test_ids_are_branch_and_path_safe(self) -> None:
        key = worktree_key("Task 1: deal ~cards^ (v2)..lock")
        assert re.fullmatch(r"[A-Za-z0-9_-]+", key)
        assert worktree_key("").startswith("task-")

    def test_awkward_task_ids_get_working_worktrees(self, manager: WorktreeManager) -> None:
        ids = [self.LONG + "-a", self.LONG + "-b", "t 3: deal ~cards^", "..", "t3.lock"]

        paths = [manager.create(worktree_key(task_id)) for task_id in ids]

        assert len(set(paths)) == len(ids)
        assert all((path / "README.md").exists() for path in paths)

    def test_workspace_path_maps_a_subdirectory_workspace(self, git_repo: Path) -> None:
        subdir = git_repo / "app"
        subdir.mkdir()
        (subdir / "main.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app"], cwd=git_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "app"], cwd=git_repo, check=True, capture_output=True
        )
        manager = WorktreeManager(repo_path=subdir, run_id="run-002")

        worktree = manager.create(worktree_key("t1"))

        assert manager.workspace_path(worktree) == worktree / "app/"
        assert (manager.workspace_path(worktree) / "main.py").is_file()
        assert WorktreeManager(git_repo, "r").workspace_path(worktree) == worktree
