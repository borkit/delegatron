"""Server run workspaces: creation, lookup, removal, and the files a run produced.

Every server run executes in ``<base>/.delegatron-runs/<run_id>``, a git repo
built from the base workspace.  When the base is the root of a git repository
the run workspace is a clone of it (same history, same ``origin``) so the run
branch can be pushed and opened as a pull request; otherwise it is a fresh
repository.  The workspace path and its initial commit are recorded in the
run's ``meta.json``; downloads, restores and cleanup find the workspace there.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from delegatron.utils.paths import (
    ALWAYS_INTERNAL_DIRS,
    internal_copy_ignore,
    internal_dirs_for,
    normalize_rel_path,
)
from delegatron.workspace import (
    GIT_IDENTITY_EMAIL,
    GIT_IDENTITY_NAME,
    RUN_BRANCH_PREFIX,
    Workspace,
    WorkspaceInitError,
)

logger = logging.getLogger(__name__)

RUNS_DIR_NAME = ".delegatron-runs"

# Regenerable tooling and VCS directories, skipped at any depth when copying.
_COPY_SKIP_PATTERNS = (".git", "__pycache__", "*.pyc", ".venv", "venv", "node_modules")
# Skipped only at the root of the base (the server's own log directory); a
# project's nested ``logs`` package (``app/logs/handlers.py``) is code.
_COPY_SKIP_ROOT_NAMES = frozenset({"logs"})

# Tooling byproducts a sandbox run can leave untracked in the run workspace
# (bytecode, installed packages, caches): never part of a run's files.
_BYPRODUCT_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
    }
)
_BYPRODUCT_SUFFIXES = (".pyc", ".pyo")
_BYPRODUCT_DIR_SUFFIXES = (".egg-info",)

_GIT_TIMEOUT_SECONDS = 600

_RMTREE_HAS_ONEXC = "onexc" in inspect.signature(shutil.rmtree).parameters

# Workspace init commits are Delegatron's own, whatever identity the host has.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": GIT_IDENTITY_NAME,
    "GIT_AUTHOR_EMAIL": GIT_IDENTITY_EMAIL,
    "GIT_COMMITTER_NAME": GIT_IDENTITY_NAME,
    "GIT_COMMITTER_EMAIL": GIT_IDENTITY_EMAIL,
}

_PATCH_SET_RE = re.compile(r"^patch_set_(\d+)\.json$")
_STATE_RE = re.compile(r"^state_(\d+)\.json$")


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
        env={**os.environ, **_GIT_IDENTITY},
    )
    if check and result.returncode != 0:
        raise WorkspaceInitError(f"git {args[0]} failed in {cwd}: {result.stderr.strip()}")
    return result


def _split_z(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]


# --- Creation -------------------------------------------------------------


def init_run_workspace(
    base: Path, target: Path, run_id: str, *, skip_paths: Iterable[Path] = ()
) -> str:
    """Create the run workspace *target* from *base*; return its initial commit SHA.

    The workspace is assembled in a sibling ``<target>.tmp`` directory and
    moved into place only when complete, so an interrupted init never leaves a
    half-built workspace to be reused.  *skip_paths* are directories under
    *base* (the server's own stores) that are never copied.
    """
    if shutil.which("git") is None:
        raise WorkspaceInitError(
            "git is not installed or not on PATH; server runs need git to create "
            "their run workspace"
        )
    staging = target.with_name(f"{target.name}.tmp")
    if staging.exists():
        remove_tree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        sha = _assemble(base, staging, run_id, skip_paths)
        os.replace(staging, target)
    except WorkspaceInitError:
        _discard(staging)
        raise
    except (OSError, subprocess.SubprocessError) as e:
        _discard(staging)
        raise WorkspaceInitError(f"Could not create workspace for run {run_id}: {e}") from e
    logger.info("Created workspace for run %s at %s", run_id, target)
    return sha


def _discard(staging: Path) -> None:
    if staging.exists():
        remove_tree(staging)


def _assemble(base: Path, staging: Path, run_id: str, skip_paths: Iterable[Path]) -> str:
    repo_root = Workspace.find_repo_root(base)
    if repo_root is not None and repo_root.resolve() == base.resolve():
        clone_args = ("--quiet", "--no-hardlinks", "--", str(base), str(staging))
        _git("clone", *clone_args, cwd=staging.parent)
        _copy_origin(base, staging)
        _continue_run_branch(staging, run_id)
        message = f"init: workspace snapshot for run {run_id}"
    else:
        staging.mkdir()
        _git("init", "--quiet", "--initial-branch=main", cwd=staging)
        message = f"init: workspace for run {run_id}"
    # Uncommitted and untracked files in the base are part of what the run starts from.
    shutil.copytree(base, staging, dirs_exist_ok=True, ignore=_copy_ignore(base, skip_paths))
    _exclude_internal_dirs(staging, internal_dirs_for(base))
    _git("add", "--all", cwd=staging)
    has_head = _git("rev-parse", "--verify", "--quiet", "HEAD", cwd=staging, check=False)
    if has_head.returncode != 0 or _git("status", "--porcelain", cwd=staging).stdout.strip():
        commit = ("commit", "--quiet", "--no-verify", "--allow-empty", "-m", message)
        _git("-c", "commit.gpgsign=false", *commit, cwd=staging)
    return _git("rev-parse", "HEAD", cwd=staging).stdout.strip()


def _copy_origin(base: Path, clone: Path) -> None:
    """Give *clone* the base's ``origin`` (so PRs push upstream), or no remote at all."""
    origin = _git("remote", "get-url", "origin", cwd=base, check=False)
    url = origin.stdout.strip()
    if origin.returncode != 0 or not url:
        # Never let a run push into the local base repository.
        _git("remote", "remove", "origin", cwd=clone)
        return
    _git("remote", "set-url", "origin", url, cwd=clone)
    # origin/HEAD names the base's checked-out branch only by its last path
    # component; without it the run's base branch is the branch checked out here.
    _git("remote", "set-head", "origin", "--delete", cwd=clone, check=False)


def _continue_run_branch(clone: Path, run_id: str) -> None:
    """Carry on another run's branch (a resumed run's seed) under this run's name.

    A run branch is never used as a base branch, so without this a workspace
    seeded from an earlier run's workspace would have no base to branch from.
    """
    head = _git("symbolic-ref", "--short", "-q", "HEAD", cwd=clone, check=False).stdout.strip()
    if head.startswith(RUN_BRANCH_PREFIX):
        _git("checkout", "--quiet", "-b", f"{RUN_BRANCH_PREFIX}{run_id}", cwd=clone)


def _copy_ignore(base: Path, skip_paths: Iterable[Path]) -> Callable[[str, list[str]], set[str]]:
    internal = internal_copy_ignore(base)
    tooling = shutil.ignore_patterns(*_COPY_SKIP_PATTERNS)
    skipped = {p.resolve() for p in skip_paths}
    skipped_names = {p.name for p in skipped}
    root = base.resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = internal(directory, names) | set(tooling(directory, names))
        if Path(directory).resolve() == root:
            ignored.update(n for n in names if n in _COPY_SKIP_ROOT_NAMES)
        ignored.update(
            n for n in names if n in skipped_names and (Path(directory) / n).resolve() in skipped
        )
        return ignored

    return _ignore


def _exclude_internal_dirs(repo: Path, internal: Iterable[str]) -> None:
    """Add root-anchored entries for the *internal* dir names to the repo's ``info/exclude``.

    *internal* is ``internal_dirs_for`` of the base the workspace was built
    from, so a user's own ``runs/`` or ``reviews/`` package stays committable.
    A directory the repository already tracks files under is left alone, as
    ``Workspace`` does, so the project's own files there stay visible.
    """
    names = sorted(internal)
    listed = _git("ls-files", "-z", "--", *names, cwd=repo).stdout
    tracked = {path.split("/", 1)[0] for path in _split_z(listed)}
    exclude = repo / _git("rev-parse", "--git-path", "info/exclude", cwd=repo).stdout.strip()
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    present = set(text.splitlines())
    missing = [f"/{name}/" for name in names if name not in tracked and f"/{name}/" not in present]
    if not missing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    separator = "\n" if text and not text.endswith("\n") else ""
    with exclude.open("a", encoding="utf-8", newline="") as fh:
        fh.write(separator + "\n".join(missing) + "\n")


# --- Lookup and removal ---------------------------------------------------


def recorded_run_workspace(run_id: str, meta: Mapping[str, Any]) -> Path | None:
    """Return the run workspace recorded in *meta*, if it has the expected shape.

    Only ``<base>/.delegatron-runs/<run_id>`` is accepted, so a tampered or
    stale ``meta.json`` can never point deletion or restore at another tree.
    """
    recorded = meta.get("run_workspace")
    if not isinstance(recorded, str) or not recorded:
        return None
    path = Path(recorded)
    if path.name != run_id or path.parent.name != RUNS_DIR_NAME:
        logger.warning("Ignoring unexpected run workspace %r for run %s", recorded, run_id)
        return None
    return path


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including git's read-only object files on Windows."""

    def _make_writable_and_retry(func: Callable[..., object], target: str, _exc: object) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    # ``onexc`` exists from Python 3.12; 3.11 (the declared minimum) has only
    # ``onerror``, whose handler takes the same three arguments.
    if _RMTREE_HAS_ONEXC:
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # pragma: no cover - Python 3.11 only
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def iteration_sha(state: Mapping[str, Any], iteration: int) -> str:
    """Workspace HEAD at the end of *iteration*, from its checkpoint ("" if unknown)."""
    sha = state.get("workspace_sha")
    if isinstance(sha, str) and sha:
        return sha
    commits = [
        message["sha"]
        for message in state.get("messages") or []
        if isinstance(message, dict)
        and message.get("event") == "workspace_commit"
        and isinstance(message.get("iteration"), int)
        and message["iteration"] <= iteration
        and isinstance(message.get("sha"), str)
    ]
    return commits[-1] if commits else ""


# --- Files a run produced -------------------------------------------------


def run_file_paths(run_dir: Path, meta: Mapping[str, Any]) -> list[str]:
    """Every file the run created or modified, across all iterations, sorted."""
    changes = _workspace_changes(run_dir, meta)
    if changes is not None:
        return changes[1]
    return list(_files_from_artifacts(run_dir))


def collect_run_files(run_dir: Path, meta: Mapping[str, Any]) -> dict[str, str]:
    """Contents of every file the run created or modified, across all iterations.

    Read from the run workspace (its diff against the workspace's initial
    commit) while it exists; otherwise rebuilt from the saved patch sets,
    limited to the files the last checkpoint records as applied.
    """
    changes = _workspace_changes(run_dir, meta)
    if changes is None:
        return _files_from_artifacts(run_dir)
    workspace, paths = changes
    files: dict[str, str] = {}
    for path in paths:
        content = _read_text(workspace, path)
        if content is not None:
            files[path] = content
    return files


def _workspace_changes(run_dir: Path, meta: Mapping[str, Any]) -> tuple[Path, list[str]] | None:
    """The run workspace and the files the run changed in it (None when it is gone).

    Committed changes since the workspace's initial commit, plus untracked
    files the run's last checkpoint records as written (all untracked files
    without one), never tooling byproducts.
    """
    workspace = recorded_run_workspace(run_dir.name, meta)
    init_sha = meta.get("workspace_init_sha")
    if workspace is None or not isinstance(init_sha, str) or not init_sha:
        return None
    if not workspace.is_dir():
        return None
    try:
        diff = _git("diff", "--name-only", "--diff-filter=d", "-z", init_sha, cwd=workspace)
        untracked = _git("ls-files", "--others", "--exclude-standard", "-z", cwd=workspace)
    except (OSError, subprocess.SubprocessError, WorkspaceInitError) as e:
        logger.warning("Could not list changes in run workspace %s: %s", workspace, e)
        return None
    # Untracked files are the run's only when its checkpoint says it wrote
    # them; tooling byproducts (bytecode, installed packages) never are.
    applied = _applied_paths(run_dir)
    new_files = [
        path
        for path in map(normalize_rel_path, _split_z(untracked.stdout))
        if path and not _is_byproduct(path) and (applied is None or path in applied)
    ]
    paths = {normalize_rel_path(p) for p in _split_z(diff.stdout)} | set(new_files)
    internal = internal_dirs_for(workspace)
    return workspace, sorted(p for p in paths if p and p.split("/", 1)[0] not in internal)


def _is_byproduct(path: str) -> bool:
    """Whether *path* is a tooling byproduct (bytecode, a cache or an installed package)."""
    *dirs, name = path.split("/")
    return name.endswith(_BYPRODUCT_SUFFIXES) or any(
        d in _BYPRODUCT_DIRS or d.endswith(_BYPRODUCT_DIR_SUFFIXES) for d in dirs
    )


def _files_from_artifacts(run_dir: Path) -> dict[str, str]:
    """The run's files rebuilt from its saved patch sets (the run workspace is gone).

    With no workspace to tell a user's ``runs/`` or ``reviews/`` package from
    Delegatron's, only ``ALWAYS_INTERNAL_DIRS`` paths are left out.
    """
    files: dict[str, str] = {}
    for patch in _numbered_json(run_dir, _PATCH_SET_RE):
        patch_files = patch.get("files") or materialize_diff(str(patch.get("diff_unified") or ""))
        for raw_path, content in patch_files.items():
            path = normalize_rel_path(raw_path)
            if path and isinstance(content, str) and not _always_internal(path):
                files[path] = content  # later iterations win
        for raw_path in patch.get("delete_files") or []:
            files.pop(normalize_rel_path(raw_path) or "", None)
    applied = _applied_paths(run_dir)
    if applied is not None:
        files = {path: content for path, content in files.items() if path in applied}
    return dict(sorted(files.items()))


def _always_internal(path: str) -> bool:
    """Whether the normalized *path* lies in one of ``ALWAYS_INTERNAL_DIRS``."""
    return path.split("/", 1)[0] in ALWAYS_INTERNAL_DIRS


def _applied_paths(run_dir: Path) -> set[str] | None:
    """Paths the last checkpoint's ``_file_history`` records as applied, if it has one."""
    states = _numbered_json(run_dir, _STATE_RE, latest_only=True)
    history = states[0].get("_file_history") if states else None
    if not isinstance(history, dict) or not history:
        return None
    return {p for p in map(normalize_rel_path, history) if p}


def _numbered_json(
    run_dir: Path, pattern: re.Pattern[str], *, latest_only: bool = False
) -> list[dict[str, Any]]:
    """Load ``<name>_NNN.json`` artifacts in numeric order (1000 follows 999)."""
    if not run_dir.is_dir():
        return []
    numbered = sorted(
        (int(match.group(1)), path)
        for path in run_dir.iterdir()
        if (match := pattern.match(path.name))
    )
    if latest_only:
        numbered = numbered[-1:]
    loaded: list[dict[str, Any]] = []
    for _, path in numbered:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Skipping unreadable artifact %s: %s", path, e)
            continue
        if isinstance(data, dict):
            loaded.append(data)
    return loaded


def _read_text(root: Path, rel_path: str) -> str | None:
    """Read a UTF-8 file under *root*; None if missing, binary, or outside *root*."""
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        return None
    try:
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def materialize_diff(diff: str) -> dict[str, str]:
    """Parse a unified diff into a files dict for new-file diffs.

    Handles the common case where the implementer produced a diff creating
    new files (--- /dev/null → +++ b/path). Returns empty dict for
    modification-only diffs that can't be materialized without the originals.
    """
    if not diff or not diff.strip():
        return {}
    files: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    is_new_file = False
    for line in diff.split("\n"):
        if line.startswith("--- "):
            is_new_file = line.strip() in ("--- /dev/null", "--- a//dev/null")
        elif line.startswith("+++ "):
            if current_path and current_lines:
                content = "\n".join(current_lines)
                files[current_path] = content if content.endswith("\n") else content + "\n"
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            current_path = path if is_new_file else None
            current_lines = []
        elif line.startswith("@@"):
            continue
        elif current_path is not None and (line.startswith("+") or line.startswith(" ")):
            current_lines.append(line[1:])
    if current_path and current_lines:
        content = "\n".join(current_lines)
        files[current_path] = content if content.endswith("\n") else content + "\n"
    return files
