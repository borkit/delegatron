"""Git workspace management for Delegatron pipeline runs.

Handles branching, file application, scoped commits, and rollback.  Each run
works on its own ``delegatron/run-<id>`` branch so the user's branches are never
committed to, and only the paths Delegatron itself wrote are ever staged.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegatron.utils.paths import internal_dirs_for, normalize_rel_path

logger = logging.getLogger(__name__)

# Identity used for run commits when git has no user.name / user.email configured.
GIT_IDENTITY_NAME = "Delegatron"
GIT_IDENTITY_EMAIL = "run@delegatron"

RUN_BRANCH_PREFIX = "delegatron/run-"

# Subject prefix of the commits a run makes on its branch.
RUN_COMMIT_PREFIX = "chore(delegatron): iteration"
_INITIAL_COMMIT_MESSAGE = "chore(delegatron): initial commit"

# Per-branch git config keys recording where a run branch was forked from.
_BASE_KEY = "delegatron-base"
_FORK_KEY = "delegatron-fork"

# The default base of ``Pipeline``/``Workspace`` callers that predate base
# resolution: used when the branch exists, otherwise resolved like no base.
_LEGACY_DEFAULT_BASE = "main"

# VCS, dependency, cache, build and IDE-state directories left out of file
# listings (other hidden directories such as ``.github`` are listed).
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".venv",
        "venv",
        ".idea",
        ".vs",
        ".cache",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".gradle",
        ".dart_tool",
        ".terraform",
    }
)

# ``rename from <path>`` lines of a git diff header (the path may be C-quoted).
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$", re.MULTILINE)
# Single-character escapes of git's C-style path quoting.
_GIT_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}
# git's per-file CRLF warnings, which would crowd the real error out of a message.
_CRLF_WARNING_RE = re.compile(r"^warning: in the working copy of .*$", re.MULTILINE)


class WorkspaceInitError(RuntimeError):
    """The run branch could not be set up; the user's checkout was left as it was."""


class DirtyWorkspaceError(WorkspaceInitError):
    """The working tree has uncommitted changes that a run branch would carry along."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        shown = ", ".join(paths[:10]) + (f" (+{len(paths) - 10} more)" if len(paths) > 10 else "")
        super().__init__(
            f"uncommitted changes in the workspace: {shown}. "
            "Commit or stash them, or allow a dirty workspace explicitly."
        )


@dataclass
class WorkspaceInfo:
    """Metadata about the current workspace state."""

    root: Path
    branch: str
    base_branch: str
    commit_sha: str
    is_clean: bool


@dataclass
class ApplyResult:
    """Outcome of ``Workspace.apply_files`` (normalized posix paths)."""

    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


@dataclass
class RenderedEdits:
    """Outcome of ``Workspace.render_edits``.

    ``files`` maps each normalized path whose edits all succeeded to its new
    full content; ``errors`` maps each path whose edits failed (the path as
    given when it is unsafe) to the reason.  A failed path contributes nothing
    to ``files``.
    """

    files: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _split_z(output: str) -> list[str]:
    """Split NUL-separated git output (``-z``) into its non-empty entries."""
    return [entry for entry in output.split("\0") if entry]


def _unquote_git_path(path: str) -> str:
    """Undo git's C-style quoting of a path in diff headers (``"a\\303\\251.py"``)."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body, out, index = path[1:-1], bytearray(), 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 == len(body):
            out += char.encode("utf-8")
            index += 1
            continue
        octal = body[index + 1 : index + 4]
        if len(octal) == 3 and all(c in "01234567" for c in octal):
            out.append(int(octal, 8) & 0xFF)
            index += 4
            continue
        out += _GIT_ESCAPES.get(body[index + 1], body[index + 1]).encode("utf-8")
        index += 2
    return out.decode("utf-8", errors="surrogateescape")


def _skip_dir(name: str, *, at_root: bool, internal: frozenset[str]) -> bool:
    return name in _SKIP_DIRS or (at_root and name in internal)


def _edit_field(edit: Any, name: str, default: Any = None) -> Any:
    """A field of an edit given as a mapping or as an object (e.g. a pydantic model)."""
    if isinstance(edit, Mapping):
        return edit.get(name, default)
    return getattr(edit, name, default)


@dataclass
class Workspace:
    """Manages a git workspace for a single pipeline run.

    Lifecycle:
    1. initialize() — check out the run branch (created from the base on first use)
    2. apply_files() / apply_patch() — write the patch, remembering the paths
    3. commit() — commit exactly those paths on the run branch
    4. rollback() — reset to the run's starting commit

    ``base_branch`` is the branch a new run branch forks from.  Empty means the
    branch checked out when ``initialize()`` runs (the detected default branch
    when HEAD is detached).
    """

    repo_path: Path
    run_id: str
    base_branch: str = ""
    run_git_hooks: bool = False  # run the repo's commit hooks on run commits
    _branch: str = field(init=False, default="")
    _initial_sha: str = field(init=False, default="")
    _original_branch: str = field(init=False, default="")
    _resumed: bool = field(init=False, default=False)
    _pending_paths: set[str] = field(init=False, default_factory=set)

    @property
    def branch(self) -> str:
        """Return the run branch name, or empty string if not yet initialized."""
        return self._branch

    @property
    def original_branch(self) -> str:
        """Branch (or detached SHA) that was checked out before ``initialize()``."""
        return self._original_branch

    @property
    def initial_sha(self) -> str:
        """Commit the run branch forked from its base."""
        return self._initial_sha

    @property
    def resumed(self) -> bool:
        """True when ``initialize()`` reused an existing run branch."""
        return self._resumed

    @staticmethod
    def find_repo_root(path: Path) -> Path | None:
        """Return the top level of the git work tree containing *path*, or ``None``.

        Unlike checking for ``path / ".git"``, this recognises a workspace that is a
        subdirectory of a repository.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        top = result.stdout.strip()
        return Path(top) if result.returncode == 0 and top else None

    @staticmethod
    def ignored_by_repo(path: Path) -> bool:
        """Whether the git repository enclosing directory *path* ignores it (``git check-ignore``).

        A run in such a folder must not use that repository: switching its
        branch would change nothing the run writes.
        """
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "."],
                cwd=path,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @staticmethod
    def checked_out_branch(path: Path) -> str:
        """The branch checked out in the git work tree at *path* ("" when detached or not git)."""
        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                cwd=path,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _detect_default_branch(self) -> str | None:
        """Detect the default branch from the remote.

        Tries ``git symbolic-ref refs/remotes/origin/HEAD`` first, then
        falls back to parsing ``git branch -r --list 'origin/HEAD'``, then to
        the current branch unless that is a Delegatron run branch.
        Returns ``None`` if detection fails (e.g. no remote configured).
        """
        try:
            result = self._git("symbolic-ref", "refs/remotes/origin/HEAD", check=False)
            if result.returncode == 0:
                # e.g. refs/remotes/origin/main; branch names may contain "/"
                return result.stdout.strip().removeprefix("refs/remotes/origin/")
        except Exception:
            pass

        try:
            result = self._git("branch", "-r", "--list", "origin/HEAD", check=False)
            if result.returncode == 0 and result.stdout.strip():
                # Typical output: "  origin/HEAD -> origin/main"
                line = result.stdout.strip()
                if "->" in line:
                    return line.split("->")[-1].strip().removeprefix("origin/")
        except Exception:
            pass

        try:
            result = self._git("branch", "--show-current", check=False)
            current = result.stdout.strip()
            if result.returncode == 0 and current and not current.startswith(RUN_BRANCH_PREFIX):
                return current
        except Exception:
            pass

        return None

    def _git(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command in the workspace (UTF-8 in and out).

        ``core.fsmonitor`` is off: a configured monitor command never runs on
        Delegatron's behalf.
        """
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=self.repo_path,
            input=input,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=check,
            timeout=60,
        )

    def _git_commit(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run ``git commit`` independent of the host's signing, hook and identity setup."""
        hooks = () if self.run_git_hooks else ("--no-verify",)
        return self._git(
            "--literal-pathspecs",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.safecrlf=false",
            "commit",
            *hooks,
            *args,
            check=check,
            env=self._commit_env(),
        )

    def _commit_env(self) -> dict[str, str]:
        """Process environment plus a fallback identity for unset user.name/email."""
        env = dict(os.environ)
        for key, suffix, value in (
            ("user.name", "NAME", GIT_IDENTITY_NAME),
            ("user.email", "EMAIL", GIT_IDENTITY_EMAIL),
        ):
            if not self._git("config", key, check=False).stdout.strip():
                env.setdefault(f"GIT_AUTHOR_{suffix}", value)
                env.setdefault(f"GIT_COMMITTER_{suffix}", value)
        return env

    def initialize(self, *, allow_dirty: bool = False, fresh: bool = False) -> WorkspaceInfo:
        """Check out this run's branch, creating it from the base on first use.

        Without *fresh* (resume, refine) an existing ``delegatron/run-<id>`` is
        checked out rather than re-created.  With *fresh* an existing branch is
        never reused: the run gets the first free ``delegatron/run-<id>-<n>``.
        A new branch forks from ``base_branch`` (a local branch, else
        ``origin/<base>``), else from the branch checked out now, else (detached
        HEAD) from the detected default branch or HEAD itself.  It is created in
        one step from the base ref, so this works in linked worktrees and never
        touches the base branch.  An unborn branch first gets an empty commit
        (the index is never committed).

        Unless *allow_dirty*, uncommitted changes (outside Delegatron's own dirs)
        raise ``DirtyWorkspaceError`` when HEAD is not already the run branch.  On
        any failure the original branch is restored, ``branch`` stays empty and
        ``WorkspaceInitError`` is raised.
        """
        run_branch = f"{RUN_BRANCH_PREFIX}{self.run_id}"
        self._branch = ""
        try:
            self._original_branch = self._current_ref()
            on_run_branch = self._original_branch == run_branch and not fresh
            if not (allow_dirty or on_run_branch):
                dirty = self._dirty_paths()
                if dirty:
                    raise DirtyWorkspaceError(dirty)

            if fresh:
                run_branch = self._free_branch_name(run_branch)
            self._resumed = self._ref_exists(f"refs/heads/{run_branch}")
            if self._resumed:
                if not on_run_branch:
                    self._git("checkout", run_branch)
                self._initial_sha = self._resumed_fork_point(run_branch)
            else:
                base_ref, base_name = self._resolve_base()
                self._git("checkout", "--no-track", "-b", run_branch, base_ref)
                self._initial_sha = self._git("rev-parse", "HEAD").stdout.strip()
                self.base_branch = base_name
                self._record_fork(run_branch, base_name, self._initial_sha)
            self._branch = run_branch
        except WorkspaceInitError:
            self._restore_original_branch()
            raise
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            self._branch = ""
            self._restore_original_branch()
            stderr = e.stderr if isinstance(e, subprocess.CalledProcessError) else ""
            detail = (stderr or "").strip() or str(e)
            raise WorkspaceInitError(f"could not set up run branch {run_branch}: {detail}") from e

        self._ensure_info_exclude()
        return self.snapshot()

    def _free_branch_name(self, run_branch: str) -> str:
        """*run_branch*, or the first ``<run_branch>-<n>`` (n >= 2) that does not exist."""
        candidate, n = run_branch, 1
        while self._ref_exists(f"refs/heads/{candidate}"):
            n += 1
            candidate = f"{run_branch}-{n}"
        return candidate

    def _resolve_base(self) -> tuple[str, str]:
        """The ref a new run branch forks from, and the base's name.

        A requested base is a local branch, else ``origin/<base>``, else any
        commit (tag or SHA).  Raises ``WorkspaceInitError`` when it is none of them.
        """
        requested = self.base_branch.strip()
        if requested and requested != _LEGACY_DEFAULT_BASE:
            ref = self._branch_ref(requested) or self._commit_sha(requested)
            if ref is None:
                raise WorkspaceInitError(f"base branch {requested!r} does not exist")
            return ref, requested
        if requested and self._ref_exists(f"refs/heads/{requested}^{{commit}}"):
            return requested, requested

        current = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        if current:
            if not self._ref_exists("HEAD"):  # unborn: give it an empty first commit
                self._create_initial_commit(current)
            return current, current
        detected = self._detect_default_branch()
        if detected:
            ref = self._branch_ref(detected)
            if ref is not None:
                return ref, detected
        head = self._git("rev-parse", "HEAD").stdout.strip()
        return head, head

    def _branch_ref(self, name: str) -> str | None:
        """The local branch *name*, else ``origin/<name>``; ``None`` when neither exists."""
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if self._ref_exists(f"{ref}^{{commit}}"):
                return ref.removeprefix("refs/heads/").removeprefix("refs/")
        return None

    def _commit_sha(self, name: str) -> str | None:
        """The commit a tag or SHA *name* names; ``None`` when it names none."""
        if name.startswith("-"):
            return None
        result = self._git("rev-parse", "--verify", "-q", f"{name}^{{commit}}", check=False)
        sha = result.stdout.strip()
        return sha if result.returncode == 0 and sha else None

    def _create_initial_commit(self, branch: str) -> None:
        """Point the unborn *branch* at an empty commit, leaving the index alone."""
        empty_tree = self._git("mktree", input="").stdout.strip()
        sha = self._git(
            "-c",
            "commit.gpgsign=false",
            "commit-tree",
            empty_tree,
            "-m",
            _INITIAL_COMMIT_MESSAGE,
            env=self._commit_env(),
        ).stdout.strip()
        # The empty old value makes this fail if the branch appeared meanwhile.
        self._git("update-ref", f"refs/heads/{branch}", sha, "")

    def _record_fork(self, run_branch: str, base_name: str, fork_sha: str) -> None:
        """Remember the base and fork point of *run_branch* (read back on resume)."""
        for key, value in ((_BASE_KEY, base_name), (_FORK_KEY, fork_sha)):
            result = self._git("config", f"branch.{run_branch}.{key}", value, check=False)
            if result.returncode != 0:
                logger.debug("Could not record %s of %s: %s", key, run_branch, result.stderr)

    def _resumed_fork_point(self, run_branch: str) -> str:
        """Where an existing run branch forked from its base.

        The recorded fork point, else the merge-base with the base branch, else
        the commit before the branch's run commits.  Without a recorded base,
        ``base_branch`` is the requested base or the detected default branch.
        """
        recorded = self._git("config", f"branch.{run_branch}.{_FORK_KEY}", check=False)
        recorded_sha = recorded.stdout.strip()
        base_name = self._git("config", f"branch.{run_branch}.{_BASE_KEY}", check=False)
        if base_name.stdout.strip():
            self.base_branch = base_name.stdout.strip()
        elif not self.base_branch:
            self.base_branch = self._detect_default_branch() or ""
        if recorded_sha and self._ref_exists(f"{recorded_sha}^{{commit}}"):
            return recorded_sha

        base_ref = self._branch_ref(self.base_branch) if self.base_branch else None
        if base_ref is not None:
            result = self._git("merge-base", base_ref, run_branch, check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        return self._before_run_commits(run_branch)

    def _before_run_commits(self, run_branch: str) -> str:
        """The first-parent ancestor of *run_branch* just before its run commits."""
        log = self._git(
            "log", "--first-parent", "--format=%H%x00%P%x00%s", run_branch, check=False
        ).stdout
        fork = ""
        for line in log.splitlines():
            sha, parents, subject = [*line.split("\0"), "", ""][:3]
            if not subject.startswith(RUN_COMMIT_PREFIX):
                return sha
            fork = parents.split(" ")[0] if parents else sha
        return fork or self._git("rev-parse", run_branch).stdout.strip()

    def _current_ref(self) -> str:
        """Current branch name, the detached HEAD SHA, or the unborn branch name."""
        head = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        return head or self._git("rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()

    def _ref_exists(self, ref: str) -> bool:
        return self._git("rev-parse", "--verify", "-q", ref, check=False).returncode == 0

    def _restore_original_branch(self) -> None:
        if self._original_branch and self._current_ref() != self._original_branch:
            self._git("checkout", self._original_branch, check=False)

    def _internal_dirs(self) -> frozenset[str]:
        """Delegatron's internal directory names at the workspace root."""
        return internal_dirs_for(self.repo_path)

    def _dirty_paths(self) -> list[str]:
        """Modified or untracked (not ignored) paths, ignoring Delegatron's own dirs."""
        excludes = [f":(exclude){name}" for name in sorted(self._internal_dirs())]
        output = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".", *excludes
        ).stdout
        entries = output.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if entry[0] in "RC":  # renames/copies are followed by their source path
                index += 1
            paths.append(entry[3:])
        return paths

    def _ensure_info_exclude(self) -> None:
        """Keep Delegatron's internal dirs out of git via ``info/exclude`` (no commit).

        Patterns are anchored to this workspace directory, so a nested
        ``src/runs/`` stays visible to git.  A directory the repo already tracks
        files under is left alone, since excluding it would hide the user's future
        files there.  Appends only missing lines, so repeated calls are no-ops.
        """
        try:
            prefix = self._git("rev-parse", "--show-prefix").stdout.strip()
            wanted: list[str] = []
            for name in sorted(self._internal_dirs()):
                if self._has_tracked_files(name):
                    logger.warning(
                        "Not excluding %r from git: the repo already tracks files there, "
                        "so Delegatron's files under it will show up as untracked.",
                        name,
                    )
                else:
                    wanted.append(f"/{prefix}{name}/")
            exclude_path = Path(self._git("rev-parse", "--git-path", "info/exclude").stdout.strip())
            if not exclude_path.is_absolute():
                exclude_path = self.repo_path / exclude_path
            existing = (
                exclude_path.read_text(encoding="utf-8", errors="replace")
                if exclude_path.exists()
                else ""
            )
            present = {line.strip() for line in existing.splitlines()}
            missing = [pattern for pattern in wanted if pattern not in present]
            if not missing:
                return
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            separator = "" if not existing or existing.endswith("\n") else "\n"
            block = separator + "# Delegatron run artifacts and scratch dirs\n" + "\n".join(missing)
            with exclude_path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(block + "\n")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("Could not update git info/exclude (non-fatal): %s", e)

    def _has_tracked_files(self, path: str) -> bool:
        """Return True when git tracks any files under ``path``."""
        return bool(self._git("ls-files", "--", path, check=False).stdout.strip())

    def snapshot(self) -> WorkspaceInfo:
        """Capture current workspace state."""
        sha_result = self._git("rev-parse", "HEAD")
        status_result = self._git("status", "--porcelain")

        return WorkspaceInfo(
            root=self.repo_path,
            branch=self._branch,
            base_branch=self.base_branch,
            commit_sha=sha_result.stdout.strip(),
            is_clean=len(status_result.stdout.strip()) == 0,
        )

    def _safe_target(self, raw_path: str) -> tuple[str, Path] | None:
        """Normalize a model-supplied path; ``None`` if it is unsafe to touch.

        Unsafe: whatever ``normalize_rel_path`` rejects (absolute, drive or UNC
        paths, ``..`` segments, ``.git`` in any spelling, ``:``, NUL, reserved
        device names), and paths that escape the workspace through a symlink.
        """
        rel = normalize_rel_path(raw_path)
        if rel is None:
            return None
        target = self.repo_path / rel
        if not target.resolve().is_relative_to(self.repo_path.resolve()):
            return None
        return rel, target

    def apply_files(
        self, files: Mapping[str, str], delete_files: Iterable[str] = ()
    ) -> ApplyResult:
        """Write complete file contents, then delete ``delete_files``.

        Every path goes through ``normalize_rel_path``.  An unsafe path, or one that
        cannot be written (including text that cannot be encoded), is skipped with
        a warning and reported in ``rejected``; it never aborts the rest of the
        patch.  Content is written as UTF-8 with the model's line endings
        preserved.  Deleting a missing file is a no-op and directories are never
        deleted.  Written and deleted paths are remembered for the next ``commit()``.
        """
        result = ApplyResult()
        for raw_path, content in files.items():
            resolved = self._safe_target(raw_path)
            if resolved is None:
                logger.warning("Rejected unsafe patch path: %r", raw_path)
                result.rejected.append(raw_path)
                continue
            rel, target = resolved
            try:
                data = content.encode("utf-8", errors="surrogateescape")
            except UnicodeEncodeError as e:
                logger.warning("Could not encode %s as UTF-8: %s", rel, e)
                result.rejected.append(raw_path)
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as e:
                logger.warning("Could not write %s: %s", rel, e)
                result.rejected.append(raw_path)
                continue
            result.written.append(rel)

        for raw_path in delete_files:
            resolved = self._safe_target(raw_path)
            if resolved is None or resolved[1].is_dir():
                logger.warning("Rejected deletion of %r", raw_path)
                result.rejected.append(raw_path)
                continue
            rel, target = resolved
            if not (target.exists() or target.is_symlink()):
                continue
            try:
                target.unlink()
            except OSError as e:
                logger.warning("Could not delete %s: %s", rel, e)
                result.rejected.append(raw_path)
                continue
            result.deleted.append(rel)

        self._pending_paths.update(result.written)
        self._pending_paths.update(result.deleted)
        return result

    def render_edits(self, edits: Iterable[Any]) -> RenderedEdits:
        """Apply search/replace *edits* to the current files in memory; nothing is written.

        Each edit has ``path``, ``search``, ``replace`` and optional ``count``
        (default 1), as a mapping or an object.  ``search`` must occur exactly
        ``count`` times in the file's current text; an edit whose ``search`` no
        longer occurs while ``replace`` occurs exactly ``count`` times is already
        applied and is a no-op (so retries are idempotent).  Edits to one path
        apply in order and succeed or fail together; any other outcome, an
        unsafe path or a missing or non-UTF-8 file fails that path only.  A file
        with CRLF line endings also matches ``search`` written with LF.
        """
        grouped: dict[str, list[Any]] = {}
        result = RenderedEdits()
        for edit in edits:
            raw_path = _edit_field(edit, "path")
            resolved = self._safe_target(raw_path) if isinstance(raw_path, str) else None
            if resolved is None:
                result.errors[str(raw_path)] = "unsafe or invalid path"
                continue
            grouped.setdefault(resolved[0], []).append(edit)

        for rel, path_edits in grouped.items():
            target = self.repo_path / rel
            try:
                text = target.read_bytes().decode("utf-8")
            except FileNotFoundError:
                result.errors[rel] = "file does not exist"
                continue
            except (OSError, UnicodeDecodeError) as e:
                result.errors[rel] = f"cannot read the file as UTF-8 text: {e}"
                continue
            for number, edit in enumerate(path_edits, 1):
                text_or_error = _apply_edit(text, edit)
                if isinstance(text_or_error, _EditError):
                    result.errors[rel] = f"edit {number}: {text_or_error.reason}"
                    break
                text = text_or_error
            else:
                result.files[rel] = text
        return result

    def diff_paths(self, diff_unified: str) -> list[str]:
        """Paths a unified diff touches, rename sources included (nothing is applied).

        Read with ``git apply --numstat``, which names a renamed file by its new
        path only; the old paths come from the diff's ``rename from`` headers.
        """
        result = self._git("apply", "--numstat", "-z", "-", input=diff_unified, check=False)
        if result.returncode != 0:
            return []
        paths: list[str] = []
        for entry in _split_z(result.stdout):
            path = entry.split("\t", 2)[-1]
            if path:
                paths.append(path)
        paths.extend(_unquote_git_path(m.strip()) for m in _RENAME_FROM_RE.findall(diff_unified))
        seen: set[str] = set()
        normalized: list[str] = []
        for rel in (normalize_rel_path(p) for p in paths):
            if rel and rel not in seen:
                seen.add(rel)
                normalized.append(rel)
        return normalized

    def apply_patch(self, diff_unified: str) -> bool:
        """Apply a unified diff to the workspace.

        Returns True if the patch applied cleanly.
        """
        if self._git("apply", "--check", "-", input=diff_unified, check=False).returncode != 0:
            return False
        touched = self.diff_paths(diff_unified)
        self._git("apply", "-", input=diff_unified)
        self._pending_paths.update(touched)
        return True

    def snapshot_files(self, paths: Iterable[str]) -> dict[str, str | None]:
        """Current text of each path (``None`` when absent), keyed by normalized path.

        Unsafe paths, and files that exist but cannot be read, are left out.
        Text is decoded as UTF-8 with universal newlines, so comparisons ignore
        CRLF vs LF.
        """
        snapshot: dict[str, str | None] = {}
        for raw_path in paths:
            resolved = self._safe_target(raw_path)
            if resolved is None:
                continue
            rel, target = resolved
            try:
                snapshot[rel] = (
                    target.read_text(encoding="utf-8", errors="replace")
                    if target.is_file()
                    else None
                )
            except OSError as e:
                logger.warning("Could not read %s for the change check: %s", rel, e)
        return snapshot

    def mark_pending(self, paths: Iterable[str]) -> None:
        """Include *paths* in the next ``commit()`` (e.g. files written before a resume)."""
        self._pending_paths.update(rel for rel in map(normalize_rel_path, paths) if rel)

    def commit(self, message: str) -> str | None:
        """Commit the paths written or deleted since the last successful commit.

        Only those paths are staged, so the user's own changes and scratch files
        never land in a run commit.  Returns the new SHA, or ``None`` when they hold
        no changes.  Raises ``RuntimeError`` when HEAD is not the run branch or git
        refuses the commit; the paths then stay pending for the next attempt.
        """
        if not self._branch:
            raise RuntimeError("refusing to commit: the run branch was never initialized")
        head = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        if head != self._branch:
            raise RuntimeError(
                f"refusing to commit: HEAD is {head or 'detached'}, not {self._branch}"
            )

        paths = self._committable(sorted(self._pending_paths))
        if paths:
            self._git("--literal-pathspecs", "-c", "core.safecrlf=false", "add", "-A", "--", *paths)
            staged = self._git(
                "--literal-pathspecs", "diff", "--cached", "--quiet", "--", *paths, check=False
            )
            if staged.returncode != 0:
                result = self._git_commit("-m", message, "--", *paths)
                if result.returncode != 0:
                    detail = _CRLF_WARNING_RE.sub("", result.stderr or result.stdout).strip()
                    raise RuntimeError(f"git commit failed (rc={result.returncode}): {detail}")
                self._pending_paths.clear()
                return self._git("rev-parse", "HEAD").stdout.strip()
        self._pending_paths.clear()
        return None

    def _committable(self, paths: list[str]) -> list[str]:
        """Pending paths git can commit: tracked, or untracked and not ignored."""
        if not paths:
            return []
        listed = self._git(
            "--literal-pathspecs",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *paths,
        ).stdout
        known = set(_split_z(listed))
        skipped = [p for p in paths if p not in known]
        if skipped:
            logger.info("Not committing ignored or absent paths: %s", skipped)
        return [p for p in paths if p in known]

    def rollback(self, to_sha: str | None = None) -> None:
        """Reset to a specific commit or the initial state."""
        target = to_sha or self._initial_sha
        if not target:
            raise RuntimeError("nothing to roll back to: the workspace was never initialized")
        self._git("reset", "--hard", target)

    def _base_range(self) -> list[str]:
        """``git diff`` arguments from the run's fork point to HEAD."""
        if self._initial_sha:
            return [self._initial_sha, "HEAD"]
        return [f"{self.base_branch or 'HEAD'}...HEAD"]

    def get_diff_from_base(self) -> str:
        """The diff of all changes committed since the run branch forked ("" on failure)."""
        result = self._git("diff", *self._base_range(), check=False)
        return result.stdout if result.returncode == 0 else ""

    def list_changed_files(self) -> list[str]:
        """Files changed since the run branch forked ([] on failure)."""
        result = self._git("diff", "--name-only", "-z", *self._base_range(), check=False)
        return _split_z(result.stdout) if result.returncode == 0 else []

    def get_file_tree(self, *, include_untracked: bool = True) -> list[str]:
        """List workspace files as sorted posix paths relative to ``repo_path``.

        In a git work tree: tracked files plus (with *include_untracked*)
        untracked files that are not ignored; dependency, cache, VCS and
        Delegatron dirs among the untracked ones are left out.  Outside git,
        when git fails, or when git lists nothing (e.g. a directory the
        enclosing repo ignores), falls back to ``get_file_tree_fallback()``.
        Never raises.
        """
        try:
            tracked = self._git("ls-files", "-z", "--cached").stdout
            deleted = self._git("ls-files", "-z", "--deleted").stdout
            untracked = (
                self._git("ls-files", "-z", "--others", "--exclude-standard").stdout
                if include_untracked
                else ""
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return self.get_file_tree_fallback()
        internal = self._internal_dirs()
        files = set(_split_z(tracked)) - set(_split_z(deleted))
        for path in _split_z(untracked):
            dirs = path.split("/")[:-1]
            if not any(_skip_dir(d, at_root=i == 0, internal=internal) for i, d in enumerate(dirs)):
                files.add(path)
        return sorted(files) if files else self.get_file_tree_fallback()

    def get_file_tree_fallback(self) -> list[str]:
        """List files by walking the disk (no git), as sorted posix paths.

        Skips VCS, dependency, cache, build and IDE-state directories (other
        hidden directories such as ``.github`` are listed) and Delegatron's
        internal directories at the workspace root.
        """
        root = self.repo_path.resolve()
        internal = self._internal_dirs()
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            at_root = rel_dir == Path()
            dirnames[:] = [
                d for d in dirnames if not _skip_dir(d, at_root=at_root, internal=internal)
            ]
            files.extend((rel_dir / name).as_posix() for name in filenames)
        return sorted(files)

    def read_file(self, path: str) -> str | None:
        """Read a file from the workspace (UTF-8).

        Raises ``ValueError`` for paths outside the workspace.
        """
        resolved = self._safe_target(path)
        if resolved is None:
            raise ValueError(f"Path traversal denied: {path}")
        target = resolved[1]
        if target.is_file():
            return target.read_text(encoding="utf-8", errors="replace")
        return None


@dataclass(frozen=True)
class _EditError:
    reason: str


def _apply_edit(text: str, edit: Any) -> str | _EditError:
    """*text* with one search/replace edit applied, or why it cannot be."""
    search = _edit_field(edit, "search")
    replace = _edit_field(edit, "replace")
    count = _edit_field(edit, "count", 1)
    if count is None:
        count = 1
    if not isinstance(search, str) or not search:
        return _EditError("'search' must be non-empty text")
    if not isinstance(replace, str):
        return _EditError("'replace' must be text")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return _EditError("'count' must be a positive integer")
    if "\r\n" in text and "\r" not in search and "\r" not in replace:
        search, replace = search.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
    found = text.count(search)
    if found == count:
        return text.replace(search, replace)
    if found == 0 and text.count(replace) == count:
        return text  # already applied
    if found == 0:
        return _EditError("'search' text not found in the file")
    return _EditError(f"'search' text found {found} times; expected exactly {count}")
