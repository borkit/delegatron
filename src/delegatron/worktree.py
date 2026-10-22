"""Git worktree isolation for parallel task execution.

Gives each task its own git worktree so that multiple implementer agents
can work concurrently without interfering with each other.  Worktrees
live under ``{repo}/.delegatron-worktrees/{run_id}/{task_id}`` and are
cleaned up automatically when the run completes.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Readable part of a worktree id: characters valid in a branch name and a path
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_ID_SLUG_LENGTH = 24


def worktree_key(task_id: str) -> str:
    """A worktree id for *task_id* that is unique, branch-name- and path-safe.

    A readable slug of the id plus a short hash of the whole id, so ids that
    share a long prefix or differ only in characters git rejects in branch
    names still get distinct worktrees.
    """
    slug = _UNSAFE_ID_CHARS.sub("-", task_id).strip("-")[:_ID_SLUG_LENGTH] or "task"
    digest = hashlib.sha1(task_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{slug}-{digest}"


@dataclass
class WorktreeInfo:
    """Metadata about a single worktree."""

    task_id: str
    path: Path
    branch: str
    exists: bool


class WorktreeError(RuntimeError):
    """Raised when a git-worktree operation fails."""


class WorktreeManager:
    """Manages git worktrees for parallel task execution within a run.

    Each task gets its own worktree branched from the current HEAD so
    that file-system operations are fully isolated across concurrent
    agents.
    """

    WORKTREE_DIR = ".delegatron-worktrees"

    def __init__(self, repo_path: Path, run_id: str) -> None:
        self.repo_path = repo_path.resolve()
        self.run_id = run_id
        self._base_dir = self.repo_path / self.WORKTREE_DIR / run_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command in the main repo (or an explicit *cwd*)."""
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo_path,
            capture_output=True,
            text=True,
            check=check,
        )

    def _branch_name(self, task_id: str) -> str:
        return f"delegatron/wt-{self.run_id}/{task_id}"

    def _worktree_path(self, task_id: str) -> Path:
        return self._base_dir / task_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, task_id: str) -> Path:
        """Create a new worktree for *task_id*, branching from HEAD.

        *task_id* names the branch and the directory; derive it with
        ``worktree_key`` from a plan's task id.  Returns the filesystem path
        of the new worktree.
        """
        wt_path = self._worktree_path(task_id)
        branch = self._branch_name(task_id)

        if wt_path.exists():
            raise WorktreeError(f"Worktree already exists for task {task_id!r} at {wt_path}")

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        result = self._git(
            "worktree",
            "add",
            "-b",
            branch,
            str(wt_path),
            "HEAD",
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree for task {task_id!r}: {result.stderr.strip()}"
            )

        logger.info("Created worktree for task %s at %s", task_id, wt_path)
        return wt_path

    def workspace_path(self, worktree: Path) -> Path:
        """Where ``repo_path`` is inside *worktree*.

        A worktree checks out the whole repository, and ``repo_path`` may be a
        subdirectory of it.
        """
        prefix = self._git("rev-parse", "--show-prefix").stdout.strip()
        return worktree / prefix if prefix else worktree

    def cleanup(self, task_id: str) -> None:
        """Remove the worktree and its branch for *task_id*."""
        wt_path = self._worktree_path(task_id)
        branch = self._branch_name(task_id)

        # Remove the worktree (--force handles uncommitted changes)
        if wt_path.exists():
            result = self._git("worktree", "remove", "--force", str(wt_path), check=False)
            if result.returncode != 0:
                logger.warning(
                    "git worktree remove failed for %s: %s",
                    task_id,
                    result.stderr.strip(),
                )

        # Prune stale worktree bookkeeping
        self._git("worktree", "prune", check=False)

        # Delete the branch
        self._git("branch", "-D", branch, check=False)

        logger.info("Cleaned up worktree for task %s", task_id)

    def cleanup_all(self) -> None:
        """Remove every worktree created for this run."""
        for info in self.list_worktrees():
            self.cleanup(info.task_id)

        # Remove the run directory if it still exists
        with contextlib.suppress(OSError):
            if self._base_dir.exists():
                self._base_dir.rmdir()

        logger.info("Cleaned up all worktrees for run %s", self.run_id)

    def list_worktrees(self) -> list[WorktreeInfo]:
        """Return metadata for every worktree belonging to this run."""
        if not self._base_dir.exists():
            return []

        infos: list[WorktreeInfo] = []
        for child in sorted(self._base_dir.iterdir()):
            if not child.is_dir():
                continue
            task_id = child.name
            branch = self._branch_name(task_id)
            infos.append(
                WorktreeInfo(
                    task_id=task_id,
                    path=child,
                    branch=branch,
                    exists=child.exists(),
                )
            )
        return infos

    def merge_into(self, task_id: str, target_branch: str) -> bool:
        """Merge the worktree branch for *task_id* into *target_branch*.

        Returns ``True`` if the merge succeeded without conflicts.
        """
        branch = self._branch_name(task_id)

        # Ensure we are on the target branch in the main repo
        checkout = self._git("checkout", target_branch, check=False)
        if checkout.returncode != 0:
            logger.error(
                "Cannot checkout target branch %s: %s",
                target_branch,
                checkout.stderr.strip(),
            )
            return False

        result = self._git("-c", "commit.gpgsign=false", "merge", "--no-ff", branch, check=False)
        if result.returncode != 0:
            logger.warning(
                "Merge of task %s into %s failed: %s",
                task_id,
                target_branch,
                result.stderr.strip(),
            )
            # Abort the failed merge so the repo is not left in a dirty state
            self._git("merge", "--abort", check=False)
            return False

        logger.info("Merged task %s into %s", task_id, target_branch)
        return True

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def worktree(self, task_id: str) -> Generator[Path, None, None]:
        """Context manager that creates a worktree and cleans it up on exit.

        Usage::

            with manager.worktree("task-1") as path:
                # work inside *path*
                ...
        """
        path = self.create(task_id)
        try:
            yield path
        finally:
            self.cleanup(task_id)
