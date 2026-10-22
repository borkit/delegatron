"""Evidence the orchestrator derives for the agents from the workspace and test history.

Pure helpers (they read workspace files, never write them):

- ``synthesize_diff`` shows the reviewer what an attempt changed, from the
  pre-patch snapshot and the files on disk (implementers return whole files,
  not diffs).
- ``flaky_from_history`` / ``flaky_flips`` find tests whose outcome flipped
  although nothing that could explain it changed since the run with the other
  outcome (any file for a test that now passes; the files a failing test
  depends on otherwise), so neither agent blames correct code for a
  nondeterministic test.
- ``select_entrypoints`` picks the task's Python scripts worth a smoke run, so
  a script that prints nothing is caught even when its tests pass.
"""

from __future__ import annotations

import ast
import difflib
import posixpath
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from delegatron.context import _local_imports
from delegatron.orchestrator.suite_guard import is_test_path, node_file
from delegatron.utils.paths import normalize_rel_path

# Most scripts smoke-run per attempt.
MAX_ENTRYPOINTS = 3

# Most workspace files followed when collecting what a test depends on.
MAX_TEST_DEPENDENCIES = 50

# Task wording that asks for a runnable script or printed output.
_SCRIPT_WORDS = re.compile(
    r"\b(?:scripts?|runs?|running|prints?|printing|cli|command[- ]line|main)\b",
    re.IGNORECASE,
)

# Python modules that are never scripts.
_NOT_SCRIPTS = frozenset({"__init__.py", "__main__.py", "conftest.py", "setup.py"})


def synthesize_diff(before: Mapping[str, str | None], after: Mapping[str, str | None]) -> str:
    """Unified diff (``a/``/``b/`` headers) of every path whose content changed.

    ``None`` means the file does not exist, shown as ``/dev/null``.  Line
    endings are normalized, so a CRLF-vs-LF difference gives no hunk.
    """
    hunks: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        lines = difflib.unified_diff(
            _lines(old),
            _lines(new),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
            lineterm="",
        )
        hunk = "\n".join(lines)
        if hunk:
            hunks.append(hunk)
    return "\n".join(hunks)


def _lines(text: str | None) -> list[str]:
    return [] if text is None else text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def flaky_from_history(
    results: Mapping[str, bool],
    test_history: Mapping[str, Sequence[Sequence[Any]]],
    file_history: Mapping[str, Mapping[str, Any]],
    *,
    workspace_root: Path | None = None,
    rerun_failed: Collection[str] = (),
) -> list[str]:
    """Node ids whose outcome flipped while nothing that could explain it changed.

    The ids of ``flaky_flips`` (see there), sorted.
    """
    flips = flaky_flips(
        results,
        test_history,
        file_history,
        workspace_root=workspace_root,
        rerun_failed=rerun_failed,
    )
    return sorted(flip["test"] for flip in flips)


def flaky_flips(
    results: Mapping[str, bool],
    test_history: Mapping[str, Sequence[Sequence[Any]]],
    file_history: Mapping[str, Mapping[str, Any]],
    *,
    workspace_root: Path | None = None,
    rerun_failed: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Tests of the latest run whose outcome differs from an earlier run on the same files.

    *results* maps node ids of the latest run to passed; *test_history* holds
    ``[iteration, "passed"|"failed"]`` runs per node id, oldest first (the
    latest run not yet included); *file_history* maps workspace paths to their
    last change (``{"iteration": n, ...}``).  A test flipped when its most
    recent run with the other outcome is not older than the last change of
    every file that could explain the flip:

    - a test that now passes: every file of the workspace (deliberately
      conservative: a fix can live in any file, e.g. a script the test runs
      in a subprocess or a data file it reads);
    - a test that now fails: with *workspace_root*, the files it depends on
      (``dependency_files``: the test file, its conftests and the workspace
      modules it imports), else every file.  A failing test the sandbox
      reran without it ever passing (*rerun_failed*) fails reproducibly and
      never counts as flipped.

    So a test that passed at it3 and it4 and fails at it5, when only
    unrelated files changed at it5, is reported (unless its reruns failed).

    Returns ``[{"test", "file", "outcome": "passed"|"failed",
    "other_outcome_iteration", "files_unchanged_since"}]`` sorted by test.
    """
    flips: list[dict[str, Any]] = []
    for nodeid, passed in sorted(results.items()):
        if not passed and nodeid in rerun_failed:
            continue
        opposite = _last_run_with(test_history.get(nodeid) or [], "failed" if passed else "passed")
        if opposite is None:
            continue
        if passed or workspace_root is None:
            watched: Iterable[str] = file_history
        else:
            watched = dependency_files(workspace_root, node_file(nodeid))
        last_change = max((_iteration(file_history.get(path)) for path in watched), default=0)
        if last_change <= opposite:
            flips.append(
                {
                    "test": nodeid,
                    "file": node_file(nodeid),
                    "outcome": "passed" if passed else "failed",
                    "other_outcome_iteration": opposite,
                    "files_unchanged_since": last_change,
                }
            )
    return flips


def dependency_files(workspace_root: Path, test_file: str) -> list[str]:
    """Workspace files a test module's outcome depends on, the test file first.

    The test file, the ``conftest.py`` files of its directory and its parents,
    and the workspace modules it imports, transitively (at most
    ``MAX_TEST_DEPENDENCIES`` files).  Paths are posix and relative; missing
    files are included only for the test file itself (a deleted test file is
    a change).
    """
    test_file = normalize_rel_path(test_file) or ""
    if not test_file:
        return []
    root = workspace_root.resolve()
    found: list[str] = [test_file]
    directory = posixpath.dirname(test_file)
    while True:
        conftest = posixpath.join(directory, "conftest.py") if directory else "conftest.py"
        if conftest != test_file and (root / conftest).is_file():
            found.append(conftest)
        if not directory:
            break
        directory = posixpath.dirname(directory)
    queue = [test_file]
    while queue and len(found) < MAX_TEST_DEPENDENCIES:
        for module in _local_imports(root, [queue.pop(0)]):
            if module not in found:
                found.append(module)
                queue.append(module)
    return found[:MAX_TEST_DEPENDENCIES]


def _last_run_with(runs: Sequence[Sequence[Any]], status: str) -> int | None:
    """Iteration of the most recent run in *runs* with *status*, or None."""
    for run in reversed(runs):
        if len(run) >= 2 and run[1] == status:
            return _iteration({"iteration": run[0]})
    return None


def _iteration(entry: Mapping[str, Any] | None) -> int:
    """The ``iteration`` of a history entry as an int (0 when missing or malformed)."""
    try:
        return int((entry or {}).get("iteration") or 0)
    except (TypeError, ValueError):
        return 0


def select_entrypoints(
    task: Mapping[str, Any], own_files: Iterable[str], workspace_root: Path
) -> list[str]:
    """The current task's Python scripts worth a smoke run, at most ``MAX_ENTRYPOINTS``.

    Candidates are the task's planned outputs and the files it wrote that are
    non-test ``.py`` files on disk (both as safe relative paths:
    ``normalize_rel_path``).  One is selected when it has a top-level
    ``if __name__ == "__main__":`` guard, or when it is a planned output
    (matched by path or by file name, since plan paths are unreliable) of a
    task whose title or description asks for a script, a CLI, a main program
    or printed output.
    """
    outputs = [p for p in map(normalize_rel_path, task.get("outputs") or []) if p]
    own = sorted(p for p in map(normalize_rel_path, own_files) if p)
    output_names = {posixpath.basename(p) for p in outputs}
    asks_for_script = bool(
        _SCRIPT_WORDS.search(f"{task.get('title', '')} {task.get('description', '')}")
    )
    selected: list[str] = []
    for path in dict.fromkeys([*outputs, *own]):
        if len(selected) == MAX_ENTRYPOINTS:
            break
        source = _script_source(workspace_root, path)
        if source is None:
            continue
        is_output = path in outputs or posixpath.basename(path) in output_names
        if (is_output and asks_for_script) or _has_main_guard(source):
            selected.append(path)
    return selected


def _script_source(root: Path, path: str) -> str | None:
    """Source of a non-test ``.py`` module that exists under *root*, else None."""
    if not path.endswith(".py") or posixpath.basename(path) in _NOT_SCRIPTS or is_test_path(path):
        return None
    target = root / path
    try:
        return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
    except OSError:
        return None


def _has_main_guard(source: str) -> bool:
    """True when the module has a top-level ``if __name__ == "__main__":`` block."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, ast.If) and _is_main_check(node.test) for node in tree.body)


def _is_main_check(test: ast.expr) -> bool:
    if not (
        isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
    ):
        return False
    sides = [test.left, *test.comparators]
    names = {s.id for s in sides if isinstance(s, ast.Name)}
    values = {s.value for s in sides if isinstance(s, ast.Constant)}
    return names == {"__name__"} and values == {"__main__"}
