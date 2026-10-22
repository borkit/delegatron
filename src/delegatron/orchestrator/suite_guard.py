"""Test-suite guards — recognise test files, protect accepted tests, excuse known failures.

Pure helpers for the orchestrator's completion gate:

- ``is_test_path`` is the single test-file predicate (all languages) used by the
  two-pass merge, smoke-test injection and plan normalization.
- ``removed_tests`` reports accepted tests that a later patch deleted from disk or
  that the latest run no longer collected, so the task cannot be approved.
- ``strip_known_tests`` / ``sibling_test_path`` keep only genuinely new tests when a
  rewrite of an existing test file would drop tests.
- ``failing_test_ids`` / ``excuse_known_failures`` let tests left failing by a
  skipped task stop blocking later, unrelated tasks.  They fail closed: a run
  whose failure is not fully explained by the failing pytest ids (collection
  error, interrupted or truncated session, another test command:
  ``attributable_failures``) excuses nothing.
"""

from __future__ import annotations

import ast
import posixpath
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any, TypeGuard

from delegatron.utils.test_output import extract_test_functions, failed_test_ids

# Directory names (any case) that hold tests at any depth, and only at the top level.
_TEST_DIRS = frozenset({"tests", "__tests__"})
_TOP_LEVEL_TEST_DIRS = frozenset({"test", "spec", "testdata"})

# .NET / Swift test project directories: "App.Tests", "App.UnitTests", "AppTests".
_TEST_PROJECT_DIR = re.compile(r"^[A-Za-z0-9_.]*[a-z0-9_.]Tests?$")

# Test file names by runner convention: pytest, Django, go, jest/vitest, rspec,
# JUnit, xUnit, Pester.
_TEST_FILE_NAME = re.compile(
    r"""^(?:
        test_.*\.py | .*_test\.py | conftest\.py | tests\.py | .*_test\.go
      | .*\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)
      | .*_spec\.rb | .*Tests?\.java | .*Test\.kt | .*Tests?\.cs | .*\.Tests\.ps1
    )$""",
    re.VERBOSE,
)

# Python test modules pytest collects by default (``python_files``).
_PYTEST_FILE_NAME = re.compile(r"^(?:test_.*|.*_test)\.py$")

_TEST_PARAMS = re.compile(r"\[.*\]$")

# pytest's closing line: "==== 1 failed, 5 passed in 0.23s ====" (with -q: no banner).
_PYTEST_RESULT_LINE = re.compile(
    r"^(?:=+ )?(?P<counts>(?:\d+ [a-z]+(?:, )?)+|no tests ran) in [\d.]+s\b.*?(?: =+)?$"
)
_PYTEST_COUNT = re.compile(r"(?P<n>\d+) (?P<kind>[a-z]+)")
_PYTEST_SESSION_START = re.compile(r"^=+ test session starts =+$", re.MULTILINE)
# "!!! Interrupted: 1 error during collection !!!" (and -x / KeyboardInterrupt stops).
_PYTEST_INTERRUPTED = re.compile(r"^(?:!+ )?Interrupted: ", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# A test command that runs pytest (optionally after ``cd``), and its chaining operators.
_PYTEST_INVOCATION = re.compile(r"^(?:python[\d.]*\s+-m\s+)?(?:\S*/)?pytest(?:\s|$)")
_COMMAND_CHAIN = re.compile(r"&&|\|\||;")


def is_test_path(path: str) -> bool:
    """True for test code and test support files (fixtures, conftest, helpers).

    A path is a test path when a directory in it is ``tests`` or ``__tests__``
    (any case, e.g. Swift's ``Tests/``) or a .NET/Swift test project
    (``App.Tests``, ``AppTests``), its first directory is ``test``, ``spec``
    or ``testdata``, it lies under ``src/test/`` (Maven/Gradle), or its file
    name follows a test-runner convention (including Django's ``tests.py``).
    ``test``/``spec`` deeper in the tree are ordinary packages.
    """
    rel = path.replace("\\", "/").removeprefix("./")
    *dirs, name = rel.split("/")
    return (
        any(part.casefold() in _TEST_DIRS or _TEST_PROJECT_DIR.match(part) for part in dirs)
        or (bool(dirs) and dirs[0] in _TOP_LEVEL_TEST_DIRS)
        or ("src", "test") in pairwise(dirs)
        or bool(_TEST_FILE_NAME.match(name))
    )


def is_pytest_file(path: str) -> bool:
    """True for a Python test module pytest collects by default (``test_*.py``, ``*_test.py``).

    Django's ``tests.py`` and test-support files are test paths that pytest
    does not collect.
    """
    return bool(_PYTEST_FILE_NAME.match(posixpath.basename(path.replace("\\", "/"))))


def node_file(nodeid: str) -> str:
    """The file part of a pytest node id (``tests/test_x.py::T::test_a`` → ``tests/test_x.py``)."""
    return nodeid.partition("::")[0]


def passing_test_ids(report: Mapping[str, Any] | None) -> list[str]:
    """Node ids of the tests that passed in *report*'s per-test results."""
    return sorted(name for name, passed in _results(report).items() if passed and "::" in name)


def removed_tests(
    accepted_ids: Iterable[str],
    sources: Mapping[str, str | None],
    report: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Accepted tests the workspace or the latest run no longer has, grouped by file.

    *sources* maps accepted Python test files to their current content (``None``
    when deleted).  A test is missing when its function is gone from that
    content, or when *report* has per-test results of a complete run that lack
    its id.  A run that did not complete (a collection error — pytest then
    stops the whole session —, an interrupted or truncated session) proves
    nothing about tests it did not reach, and skipped runs have no results, so
    only the static check applies to them.

    Returns ``[{"file", "removed": [names without the file part]}]`` sorted by file.
    """
    ran = _results(report) if _run_completed(report) else {}
    on_disk = {
        file: extract_test_functions(source) if source is not None else set()
        for file, source in sources.items()
    }
    missing: dict[str, set[str]] = {}
    for nodeid in accepted_ids:
        file, _, name = nodeid.partition("::")
        if not name:
            continue
        gone = file in on_disk and _TEST_PARAMS.sub("", name) not in on_disk[file]
        if not gone and ran:
            gone = nodeid not in ran
        if gone:
            missing.setdefault(file, set()).add(name)
    return [{"file": file, "removed": sorted(names)} for file, names in sorted(missing.items())]


def strip_known_tests(source: str, known: Collection[str]) -> str | None:
    """Return *source* without the tests named in *known* (``test_x``, ``Class::test_x``).

    Nested classes chain with ``::`` (``Outer::Inner::test_x``), as
    ``extract_test_functions`` names them.  Classes left without tests are
    dropped; everything else (imports, fixtures, helpers) is kept.  Returns
    ``None`` when no test remains or *source* does not parse.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    tree.body = _strip_body(tree.body, "", known)
    stripped = ast.unparse(tree) + "\n"
    return stripped if extract_test_functions(stripped) else None


def _strip_body(body: list[ast.stmt], prefix: str, known: Collection[str]) -> list[ast.stmt]:
    """*body* (of the module or of the class named by *prefix*) without the *known* tests."""
    kept: list[ast.stmt] = []
    for node in body:
        if _is_function(node) and f"{prefix}{node.name}" in known:
            continue
        if isinstance(node, ast.ClassDef):
            members = _strip_body(node.body, f"{prefix}{node.name}::", known)
            if len(members) != len(node.body) or members != node.body:
                if not _has_tests(members):
                    continue
                node.body = members
        kept.append(node)
    return kept


def _has_tests(body: list[ast.stmt]) -> bool:
    """True when *body* defines a ``test*`` function, directly or in a nested class."""
    return any(
        (_is_function(node) and node.name.startswith("test"))
        or (isinstance(node, ast.ClassDef) and _has_tests(node.body))
        for node in body
    )


def sibling_test_path(path: str, task_id: str) -> str:
    """A new test file beside *path* for *task_id*, still matching the runner's name pattern.

    ``tests/test_deck.py`` → ``tests/test_deck_t2.py``; ``deck_test.py`` → ``deck_t2_test.py``.
    """
    stem, ext = posixpath.splitext(path)
    suffix = re.sub(r"\W", "_", task_id) or "task"
    if stem.endswith("_test"):
        return f"{stem[: -len('_test')]}_{suffix}_test{ext}"
    return f"{stem}_{suffix}{ext}"


def failing_test_ids(report: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Ids of the tests a run failed: empty when it passed, ``None`` when unattributable.

    A failed run without failing pytest test ids (install error, parse gate,
    skipped sandbox, non-pytest output) cannot be attributed to tests, nor can
    a collection error: pytest reports the module (an id without ``::``) and
    stops the whole session (``Interrupted:``), so no other test ran.
    """
    if report is not None and report.get("passed"):
        return frozenset()
    if not report or report.get("skipped_reason"):
        return None
    output = _output(report)
    ids = report.get("failed_tests") or failed_test_ids(output)
    if not ids or any("::" not in str(nodeid) for nodeid in ids):
        return None
    if _PYTEST_INTERRUPTED.search(output):
        return None
    return frozenset(str(nodeid) for nodeid in ids)


def attributable_failures(
    report: Mapping[str, Any] | None,
    *,
    test_commands: Sequence[str] | None = None,
) -> frozenset[str] | None:
    """``failing_test_ids`` when they fully explain the run's failure, else ``None``.

    Fails closed (``None``) also for a session that never printed its result
    line (timeout, truncation), more failures counted than ids found, output
    after pytest's result line (another command ran), a non-pytest exit code
    (pytest exits 1 for failed tests), or test commands that are not all
    pytest (*test_commands*, else the report's own ``test_commands``: a second
    command such as ``mypy .`` or ``npm test`` may be what failed, or never
    ran).  A report without output (built by the caller) is judged on its ids.
    """
    ids = failing_test_ids(report)
    if not ids or report is None:
        return ids
    commands = test_commands if test_commands is not None else report.get("test_commands")
    if commands is not None and not _only_pytest(commands):
        return None
    exit_code = report.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 1:
        return None
    if _output(report).strip() and not _explained(report, len(ids)):
        return None
    return ids


def excuse_known_failures(
    report: Mapping[str, Any],
    known_failures: Mapping[str, str],
    own_files: Collection[str],
    *,
    test_commands: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Split a failed run into new failures and failures known before the current task.

    A failure is excused when its id is in *known_failures* (left failing by a
    skipped task) and its test file is not one of the current task's own files.
    The targeted run of the task's own tests (``report["targeted"]``) must fail
    only on excusable ids too: a targeted failure on another id is a new
    failure, and one without ids fails the task.  Returns the report fields
    ``new_failures``, ``baseline_failures`` and ``task_passed`` (no new
    failure), or ``None`` when nothing is excused or the failure cannot be
    attributed to test ids (``attributable_failures``; *test_commands* are the
    run's test commands).
    """
    ids = attributable_failures(report, test_commands=test_commands)
    if not ids:
        return None

    def excusable(nodeid: str) -> bool:
        return nodeid in known_failures and node_file(nodeid) not in own_files

    excused = sorted(i for i in ids if excusable(i))
    if not excused:
        return None
    new = set(ids.difference(excused))
    targeted_ok = True
    targeted = report.get("targeted")
    if isinstance(targeted, Mapping) and not targeted.get("passed", True):
        targeted_ids = [str(i) for i in targeted.get("failing_ids") or []]
        targeted_ok = bool(targeted_ids)
        new.update(i for i in targeted_ids if not excusable(i))
    return {
        "new_failures": sorted(new),
        "baseline_failures": excused,
        "task_passed": targeted_ok and not new,
    }


def _output(report: Mapping[str, Any]) -> str:
    """The run's stdout and stderr, without ANSI escapes."""
    return _ANSI.sub("", f"{report.get('stdout') or ''}\n{report.get('stderr') or ''}")


def _explained(report: Mapping[str, Any], id_count: int) -> bool:
    """Whether a failed pytest run is fully explained by its *id_count* failing ids.

    The session completed (its result line is in stdout, it was not
    interrupted), it counted no more failures and errors than there are ids,
    and nothing but blank lines follows its result line in stdout (no other
    command printed after it).
    """
    if _PYTEST_INTERRUPTED.search(_output(report)):
        return False
    lines = _ANSI.sub("", report.get("stdout") or "").splitlines()
    results = [
        match
        for match in (_PYTEST_RESULT_LINE.match(line.strip()) for line in lines)
        if match is not None
    ]
    if not results:
        return False
    counted = sum(
        int(count.group("n"))
        for match in results
        for count in _PYTEST_COUNT.finditer(match.group("counts"))
        if count.group("kind") in ("failed", "error", "errors")
    )
    if counted > id_count:
        return False
    last = max(i for i, line in enumerate(lines) if _PYTEST_RESULT_LINE.match(line.strip()))
    return not any(line.strip() for line in lines[last + 1 :])


def _run_completed(report: Mapping[str, Any] | None) -> bool:
    """Whether *report*'s per-test results cover the whole suite.

    False after a collection error (a module id without ``::``: pytest stops
    the whole session), an interrupted session, or output whose pytest
    session never printed its result line (timeout, truncation).
    """
    if not report:
        return False
    if any("::" not in name for name in _results(report)):
        return False
    output = _output(report)
    if _PYTEST_INTERRUPTED.search(output):
        return False
    if _PYTEST_SESSION_START.search(output) or failed_test_ids(output):
        return any(_PYTEST_RESULT_LINE.match(line.strip()) for line in output.splitlines())
    return True


def _only_pytest(commands: Sequence[str] | str) -> bool:
    """True when every test command runs pytest (``cd`` steps aside): pytest explains a failure."""
    if isinstance(commands, str):
        commands = [commands]
    steps = [
        step.strip()
        for command in commands
        for step in _COMMAND_CHAIN.split(str(command))
        if step.strip()
    ]
    runs = [step for step in steps if not step.startswith("cd ")]
    return bool(runs) and all(_PYTEST_INVOCATION.match(step) for step in runs)


def _results(report: Mapping[str, Any] | None) -> dict[str, bool]:
    """Per-test results of a run as ``{nodeid: passed}`` (empty for skipped runs)."""
    if not report or report.get("skipped_reason"):
        return {}
    return {
        str(result["name"]): bool(result.get("passed"))
        for result in report.get("results") or []
        if isinstance(result, Mapping) and result.get("name")
    }


def _is_function(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
