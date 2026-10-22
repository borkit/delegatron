"""Lines a patch adds, relative to the workspace it will be applied to.

The security checks judge only what a patch introduces.  Implementers
return whole files, so each ``files`` entry is compared with the current
workspace copy: lines that were already there are not attributed to the
patch, and an entry identical to the workspace copy is not a change at all.
Run the checks before the patch is applied, while the workspace still holds
the baseline.

Across iterations the workspace already holds earlier (applied) patches, so
a :class:`SecurityBaseline` keeps what each file held before the run first
changed it; diffing against that keeps a flagged line attributed to the run
until it is removed.
"""

from __future__ import annotations

import codecs
import logging
import re
import subprocess
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from delegatron.utils.paths import normalize_rel_path

logger = logging.getLogger(__name__)

# path -> [(1-based line number after the patch is applied, line text)]
AddedLines = dict[str, list[tuple[int, str]]]

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_GIT_TIMEOUT_SECONDS = 30


def decode_text(data: bytes) -> str:
    """Decode file bytes: a UTF-16 or UTF-8 BOM is honoured, anything else is UTF-8."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def patch_files(patch: Mapping[str, Any]) -> dict[str, str]:
    """The patch's ``files`` keyed by normalized posix path.

    Entries with a non-string path or content, or a path that is absolute or
    escapes the workspace, are dropped: they are never written.
    """
    files = patch.get("files")
    if not isinstance(files, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_path, content in files.items():
        path = normalize_rel_path(raw_path)
        if path is not None and isinstance(content, str):
            result[path] = content
    return result


def resolve_edits(patch: Mapping[str, Any], workspace_root: Path | None) -> dict[str, Any]:
    """Return *patch* with its search/replace ``edits`` folded into ``files``.

    Each edit (``{path, search, replace, count=1}``) is applied to the patch's
    own ``files`` entry for that path, else to the workspace copy, so the
    checks see the content that will be written.  An edit whose ``search``
    text is not found exactly, or that names an unsafe path, is left out:
    the orchestrator rejects it too.  A patch without edits is returned as is.
    """
    edits = patch.get("edits")
    if not isinstance(edits, list) or not edits:
        return dict(patch)
    files = patch_files(patch)
    for edit in edits:
        if not isinstance(edit, Mapping):
            continue
        raw_path = edit.get("path")
        path = normalize_rel_path(raw_path) if isinstance(raw_path, str) else None
        search, replace = edit.get("search"), edit.get("replace")
        if path is None or not isinstance(search, str) or not isinstance(replace, str):
            continue
        current = files[path] if path in files else _read_baseline(workspace_root, path)
        if not search or current is None or search not in current:
            continue
        count = edit.get("count", 1)
        count = count if isinstance(count, int) and count > 0 else 1
        files[path] = current.replace(search, replace, count)
    return {**patch, "files": files}


def added_lines(
    patch: Mapping[str, Any],
    workspace_root: Path | None = None,
    *,
    baseline: SecurityBaseline | None = None,
) -> AddedLines:
    """Return the lines *patch* adds or modifies, per changed file.

    A ``files`` entry is diffed line by line against the workspace copy (or,
    for a path *baseline* has recorded, against that path's original); every
    line counts as added when the file is new or *workspace_root* is
    ``None``.  Entries with the same lines as the copy they are compared with
    (line endings aside) are omitted, and a file whose change only deletes
    lines maps to an empty list.  ``diff_unified`` hunks contribute their
    ``+`` lines for files that ``files`` does not already cover; with a
    *baseline* they are added to the lines of a file it widened in.
    """
    result: AddedLines = {}
    for path, content in patch_files(patch).items():
        new_lines = content.splitlines()
        if baseline is not None and baseline.has(path):
            original = baseline.original(path)
        else:
            original = _read_baseline(workspace_root, path)
        if original is None:
            result[path] = list(enumerate(new_lines, start=1))
        elif (old_lines := original.splitlines()) != new_lines:
            result[path] = _inserted_lines(old_lines, new_lines)

    diff = patch.get("diff_unified")
    if isinstance(diff, str) and diff:
        for path, lines in _diff_added_lines(diff).items():
            if baseline is not None and path in result:
                result[path] = sorted(set(result[path]) | set(lines))
            else:
                result.setdefault(path, lines)
    return result


class SecurityBaseline:
    """What each file held before the run (or task) first changed it.

    Create one per run (or per task) and pass it to every
    ``run_security_checks(..., baseline=...)`` call.  Each call then judges
    everything changed since the baseline instead of only the difference
    from the previous iteration: a secret, forbidden pattern or protected-file
    edit that an earlier, already applied patch introduced is reported again
    on every pass, whether the implementer re-sends the file or leaves it
    alone, until the line is removed or the file restored.

    :meth:`record` snapshots a path's workspace content the first time a
    patch touches it, so it must run before each patch is applied
    (``run_security_checks`` does this).  After a resume, pass the paths the
    run had already changed as *changed_paths*; their originals are read
    from *git_ref* (for example the run branch's fork point), or from the
    workspace when no usable ref is given.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        changed_paths: Iterable[str] = (),
        git_ref: str = "",
    ) -> None:
        self._root = workspace_root
        self._originals: dict[str, str | None] = {}
        ref = git_ref if git_ref and _git_ref_exists(workspace_root, git_ref) else ""
        if git_ref and not ref:
            logger.warning("Security baseline: git ref %r not found; using the workspace", git_ref)
        for raw_path in changed_paths:
            path = normalize_rel_path(raw_path)
            if path is None or path in self._originals:
                continue
            if ref:
                self._originals[path] = _read_git_blob(workspace_root, ref, path)
            else:
                self._originals[path] = _read_baseline(workspace_root, path)

    @property
    def paths(self) -> list[str]:
        """Every path recorded so far (for checkpointing), sorted."""
        return sorted(self._originals)

    def has(self, path: str) -> bool:
        """True when *path* (normalized) has a recorded original."""
        return path in self._originals

    def original(self, path: str) -> str | None:
        """The recorded original text of *path*; ``None`` when it did not exist."""
        return self._originals.get(path)

    def record(self, patch: Mapping[str, Any]) -> None:
        """Snapshot the current content of every path *patch* touches, once per path."""
        for path in touched_paths(patch):
            if path not in self._originals:
                self._originals[path] = _read_baseline(self._root, path)

    def widen(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        """*patch* plus every other recorded path that still differs from its original.

        A recorded file the patch does not mention is added to ``files`` with
        its current workspace content, or to ``delete_files`` when it existed
        originally and is gone now.  Paths the patch writes, deletes or diffs
        keep the patch's own version.
        """
        files = patch_files(patch)
        deletes = {
            path for path in map(normalize_rel_path, _str_items(patch.get("delete_files"))) if path
        }
        diffed = _diff_paths(patch.get("diff_unified"))
        for path, original in self._originals.items():
            if path in files or path in deletes:
                continue
            current = _read_baseline(self._root, path)
            if current is not None and (path not in diffed or current != original):
                files[path] = current
            elif current is None and original is not None and path not in diffed:
                deletes.add(path)
        return {**patch, "files": files, "delete_files": sorted(deletes)}


def touched_paths(patch: Mapping[str, Any]) -> list[str]:
    """Normalized paths *patch* writes, deletes, edits or diffs."""
    paths = list(patch_files(patch))
    paths.extend(p for p in map(normalize_rel_path, _str_items(patch.get("delete_files"))) if p)
    edits = patch.get("edits")
    if isinstance(edits, list):
        paths.extend(
            p
            for p in (
                normalize_rel_path(e["path"])
                for e in edits
                if isinstance(e, Mapping) and isinstance(e.get("path"), str)
            )
            if p
        )
    paths.extend(_diff_paths(patch.get("diff_unified")))
    return list(dict.fromkeys(paths))


def _str_items(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _read_baseline(workspace_root: Path | None, rel_path: str) -> str | None:
    """Current workspace text of *rel_path*, or ``None`` when there is none."""
    if workspace_root is None:
        return None
    target = workspace_root / rel_path
    try:
        return decode_text(target.read_bytes()) if target.is_file() else None
    except OSError:
        return None


def _git(workspace_root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace_root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_ref_exists(workspace_root: Path, ref: str) -> bool:
    if ref.startswith("-"):
        return False
    result = _git(workspace_root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return result is not None and result.returncode == 0


def _read_git_blob(workspace_root: Path, ref: str, rel_path: str) -> str | None:
    """Text of *rel_path* (relative to *workspace_root*) at *ref*; ``None`` when absent."""
    result = _git(workspace_root, "cat-file", "blob", f"{ref}:./{rel_path}")
    if result is None or result.returncode != 0:
        return None
    return decode_text(result.stdout)


def _inserted_lines(old_lines: list[str], new_lines: list[str]) -> list[tuple[int, str]]:
    """Lines of *new_lines* that are not unchanged lines of *old_lines*."""
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    return [
        (index + 1, new_lines[index])
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
        if tag in ("replace", "insert")
        for index in range(j1, j2)
    ]


def _diff_target(header: str, prefix: str = "b/") -> str | None:
    """Workspace path named by a diff header, or ``None`` for ``/dev/null``.

    *prefix* is the side's git prefix: ``b/`` for ``+++``, ``a/`` for ``---``.
    """
    target = header.split("\t", 1)[0].strip()
    if target == "/dev/null":
        return None
    return normalize_rel_path(target.removeprefix(prefix))


def _diff_paths(diff: Any) -> list[str]:
    """Paths a unified diff creates, changes or deletes."""
    if not isinstance(diff, str) or not diff:
        return []
    lines = diff.splitlines()
    paths: list[str | None] = []
    for line, following in zip(lines, [*lines[1:], ""], strict=True):
        if line.startswith("+++ "):
            paths.append(_diff_target(line[4:], "b/"))
        elif line.startswith("--- ") and following.startswith("+++ "):
            # A "---" line is a header only right before its "+++" line; inside
            # a hunk it is a removed line that starts with "--".
            paths.append(_diff_target(line[4:], "a/"))
    return list(dict.fromkeys(p for p in paths if p))


def _diff_added_lines(diff: str) -> AddedLines:
    """``+`` lines per file of a unified diff, numbered from the hunk headers.

    Diffs without ``@@`` headers are numbered from 1.
    """
    result: AddedLines = {}
    current: list[tuple[int, str]] | None = None
    lineno = 1
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = _diff_target(line[4:])
            current = result.setdefault(path, []) if path else None
            lineno = 1
        elif hunk := _HUNK_RE.match(line):
            lineno = int(hunk.group(1))
        elif current is None or line.startswith(("-", "\\")):
            continue
        elif line.startswith("+"):
            current.append((lineno, line[1:]))
            lineno += 1
        else:
            lineno += 1
    return result
