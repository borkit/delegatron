"""Shared path helpers for LLM-supplied paths and Delegatron's own scratch directories.

Every component that writes, copies, or lists workspace files uses these so the
rules live in one place:

- ``normalize_rel_path`` turns a model-supplied path into a safe, posix,
  workspace-relative path (or rejects it) before anything touches the disk.
- ``INTERNAL_DIRS`` / ``internal_dirs_for`` / ``is_internal_path`` /
  ``internal_copy_ignore`` identify Delegatron's artifact and scratch
  directories at the workspace root so they are never committed, copied into
  sandboxes, or shown to agents.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

# The server's review store (``/api/review`` reports) at the workspace root.
REVIEWS_DIR_NAME = "reviews"

# Root-level directories Delegatron creates inside a workspace.
INTERNAL_DIRS: tuple[str, ...] = (
    "runs",
    "_sandbox",
    REVIEWS_DIR_NAME,
    ".delegatron-runs",
    ".delegatron-worktrees",
    ".delegatron-venv",
)

# Common names a user project may also use; internal only when the directory
# holds Delegatron's own layout (see ``internal_dirs_for``).
_SHARED_NAMES: tuple[str, ...] = ("runs", REVIEWS_DIR_NAME)
# Files that mark a ``runs/<id>/`` child as a Delegatron run directory, plus
# its iteration checkpoints (``state_001.json``, see ``ArtifactStore``).
_RUN_MARKERS: tuple[str, ...] = ("summary.json", "events.jsonl")
_CHECKPOINT_RE = re.compile(r"^state_\d+\.json$")
_REVIEW_DIR_PREFIX = "review-"

# Always Delegatron's own when present at the root.
ALWAYS_INTERNAL_DIRS: frozenset[str] = frozenset(INTERNAL_DIRS).difference(_SHARED_NAMES)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Windows reserved device names; reserved with any extension (``nul.txt``).
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in "123456789¹²³"}
    | {f"lpt{n}" for n in "123456789¹²³"}
)


def _is_unsafe_component(part: str) -> bool:
    """True for a path component that must never be written on any host OS.

    Covers ``.git`` in any case or with trailing dots/spaces (case-insensitive
    and dot-stripping filesystems map them onto the real ``.git``), its 8.3
    short name ``git~1``, NTFS alternate data streams (``:``), NUL and the
    reserved Windows device names.
    """
    if "\0" in part or ":" in part:
        return True
    folded = part.casefold().rstrip(" .")
    if folded in (".git", "git~1"):
        return True
    stem = folded.split(".", 1)[0].rstrip(" ")
    return stem in _RESERVED_DEVICE_NAMES


def normalize_rel_path(path: str) -> str | None:
    """Return *path* as a clean posix path relative to the workspace root.

    Backslashes become ``/``, ``./`` prefixes and duplicate separators are
    dropped.  Returns ``None`` for anything that is empty, absolute (posix
    root, drive letter, UNC), escapes the root via ``..``, or has a component
    that is unsafe on some host OS (``.git`` in any spelling, ``git~1``, ``:``,
    NUL, reserved device names such as ``CON`` or ``nul.txt``).
    """
    if not isinstance(path, str):
        return None
    candidate = path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or _DRIVE_RE.match(candidate):
        return None
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(_is_unsafe_component(p) for p in parts):
        return None
    return str(PurePosixPath(*parts))


def _looks_like_delegatron_dir(directory: Path, name: str) -> bool:
    """Whether ``runs/`` or ``reviews/`` at a workspace root holds Delegatron's layout."""
    try:
        children = [child for child in directory.iterdir() if child.is_dir()]
    except OSError:
        return False
    if name == REVIEWS_DIR_NAME:
        return any(child.name.startswith(_REVIEW_DIR_PREFIX) for child in children)
    return any(_is_run_dir(child) for child in children)


def _is_run_dir(directory: Path) -> bool:
    """Whether *directory* holds a run's ``summary.json``, ``events.jsonl`` or checkpoints."""
    if any((directory / marker).is_file() for marker in _RUN_MARKERS):
        return True
    try:
        return any(
            _CHECKPOINT_RE.match(entry.name) and entry.is_file() for entry in directory.iterdir()
        )
    except OSError:
        return False


def internal_dirs_for(root: Path) -> frozenset[str]:
    """Delegatron's internal directory names at the root of *root*.

    ``ALWAYS_INTERNAL_DIRS`` always count.  ``runs`` and ``reviews`` count only
    when they look like Delegatron's (a child dir holding ``summary.json``,
    ``events.jsonl`` or a ``state_NNN.json`` checkpoint; a ``reviews/review-*``
    child), so a user package with one of those names stays user code.
    """
    names = set(ALWAYS_INTERNAL_DIRS)
    root_path = Path(root)
    for name in _SHARED_NAMES:
        directory = root_path / name
        if directory.is_dir() and _looks_like_delegatron_dir(directory, name):
            names.add(name)
    return frozenset(names)


def is_internal_path(rel_path: str, root: Path | None = None) -> bool:
    """True when the first component of *rel_path* is a Delegatron directory.

    With *root*, ``internal_dirs_for(root)`` decides; without one every name in
    ``INTERNAL_DIRS`` counts.
    """
    normalized = normalize_rel_path(rel_path)
    if normalized is None:
        return False
    internal = internal_dirs_for(root) if root is not None else INTERNAL_DIRS
    return normalized.split("/", 1)[0] in internal


def internal_copy_ignore(
    src_root: Path, extra: Iterable[str] = ()
) -> Callable[[str, list[str]], set[str]]:
    """Build a ``shutil.copytree`` ignore callable for *src_root*.

    ``internal_dirs_for(src_root)`` are dropped only at the root (a nested
    ``src/runs/`` package is user code); names in *extra* are dropped at any
    depth.
    """
    root = Path(src_root).resolve()
    internal = internal_dirs_for(root)
    extra_names = set(extra)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {n for n in names if n in extra_names}
        if Path(directory).resolve() == root:
            ignored.update(n for n in names if n in internal)
        return ignored

    return _ignore
