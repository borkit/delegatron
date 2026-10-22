"""Abstract base class for sandbox runners, plus helpers shared by the runners."""

from __future__ import annotations

import logging
import re
import shlex
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delegatron.schemas.test_report import EntrypointRun, SandboxTestResult
from delegatron.utils.paths import normalize_rel_path
from delegatron.utils.test_output import build_results as parse_test_results
from delegatron.utils.test_output import (
    failed_test_ids,
    format_test_failures,
)

logger = logging.getLogger(__name__)

# Shell metacharacters, and control characters other than tab: a newline or
# carriage return would start a second command in the shell.
_DANGEROUS_PATTERNS = re.compile(r"[;`$|><\\&\x00-\x08\x0a-\x1f\x7f]|\.\./")

# Most per-test results kept in a report; failing ones are kept first.
MAX_RESULTS = 1000

# Node ids safe to put on a rerun command line: a relative .py path that does
# not start like an option, plus "::" test names and simple parametrize ids —
# no spaces or shell metacharacters.  ".." segments are refused separately.
_RERUNNABLE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(::[A-Za-z0-9_.\[\]=,-]+)+$")

# Words the rerun may start with to call pytest the way the test run did
# ("pytest", "python -m pytest", ".venv/bin/pytest"): no shell syntax.
_RERUN_PREFIX_WORD = re.compile(r"^[A-Za-z0-9_./-]+$")
_PYTEST_WORD = re.compile(r"(?:^|/)pytest$")

# Workspace-relative scripts safe to smoke-run (no absolute or option-like paths)
_ENTRYPOINT_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py$")

# Most failed tests worth a flakiness rerun; more points at a real breakage.
MAX_RERUN_TESTS = 10

# Reruns of the still-failing tests when the patch does not say otherwise.
DEFAULT_FLAKY_RERUNS = 2
MAX_FLAKY_RERUNS = 5

# Seconds each entrypoint smoke run may take before it is killed.
ENTRYPOINT_TIMEOUT = 10

_ENTRYPOINT_OUTPUT_CHARS = 2000


class UnsafeCommandError(ValueError):
    """An agent-supplied shell command contains shell metacharacters."""


def sanitize_shell_command(cmd: str) -> str:
    """Return a shell command from agent output if it is safe to run.

    Allows normal commands like 'python -m pytest -v' or
    'pip install -e ".[dev]"' and raises :class:`UnsafeCommandError` for
    anything containing shell metacharacters or control characters (a
    newline would run a second command), so a blocked command never runs in
    any form.  Infrastructure commands (factory, project detection,
    toolchain rules) are trusted and are not passed through here.
    """
    cmd = cmd.strip()
    if not cmd:
        return "true"  # no-op

    if _DANGEROUS_PATTERNS.search(cmd):
        logger.warning("Blocked potentially unsafe command: %r", cmd)
        raise UnsafeCommandError(f"Blocked unsafe command: {cmd}")

    return cmd


def build_results(output: str, cap: int = MAX_RESULTS) -> list[SandboxTestResult]:
    """One result per pytest node id in *output*, at most *cap* of them.

    A failed or errored test carries the first ``E`` line of its failure
    section as ``error`` and every result its pytest outcome as ``status``
    (``passed``, ``failed``, ``error``, ``skipped``, ``xfailed``,
    ``xpassed``).  Over *cap*, failing results are kept first, then passing
    ones, in the order pytest reported them (``test_output.build_results``).
    """
    return [SandboxTestResult.model_validate(row) for row in parse_test_results(output, cap)]


def per_test_fields(output: str, passed: bool) -> dict[str, Any]:
    """Return the per-test ``SandboxReport`` fields for a test run.

    *output* must be the full stdout + stderr, before any truncation.
    Gives ``results`` (see :func:`build_results`), ``failed_tests`` and, for
    a failed run, ``failure_excerpt``.
    """
    return {
        "results": build_results(output),
        "failed_tests": failed_test_ids(output),
        "failure_excerpt": "" if passed else format_test_failures(output),
    }


def report_extras(stdout: str, stderr: str, kept_stdout: int, kept_stderr: int) -> dict[str, Any]:
    """Report fields about the run's output: ``output_truncated`` when either stream was cut."""
    return {"output_truncated": len(stdout) > kept_stdout or len(stderr) > kept_stderr}


# ---------------------------------------------------------------------------
# Flaky-test reruns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RerunPlan:
    """Which failed tests a run reruns to spot flakes, and how.

    ``ids`` is empty when there is no rerun; ``skipped`` then says why
    (``disabled``, ``not_pytest``, ``no_ids``, ``too_many``, ``unsafe_id``;
    the Docker sandbox adds ``windows_image``), or is ``""`` for a run that
    passed.  ``prefix`` starts pytest the way the test command did and
    ``attempts`` is the most reruns allowed.
    """

    ids: tuple[str, ...] = ()
    prefix: tuple[str, ...] = ()
    attempts: int = 0
    skipped: str = ""


@dataclass(frozen=True)
class RerunResult:
    """What the reruns found: the ``flaky`` ids (failed, then passed) and reruns made."""

    flaky: list[str]
    attempts: int


def plan_rerun(
    patch: Mapping[str, Any],
    test_commands: list[str],
    passed: bool,
    failed_tests: list[str],
) -> RerunPlan:
    """Decide whether a failed run's tests are rerun for flakiness.

    A rerun applies when the patch allows it (``flaky_rerun``, default on;
    ``flaky_reruns`` reruns, default 2), the test phase was a single pytest
    command (so the rerun covers everything that ran, and its invocation
    can be mirrored), and there are 1 to ``MAX_RERUN_TESTS`` failed ids,
    each safe to pass on a command line.
    """
    if passed:
        return RerunPlan()
    attempts = rerun_count(patch)
    if attempts <= 0:
        return RerunPlan(skipped="disabled")
    prefix = pytest_prefix(test_commands[0]) if len(test_commands) == 1 else None
    if prefix is None:
        return RerunPlan(skipped="not_pytest")
    if not failed_tests:
        return RerunPlan(skipped="no_ids")
    if len(failed_tests) > MAX_RERUN_TESTS:
        return RerunPlan(skipped="too_many")
    if not all(map(_rerunnable, failed_tests)):
        return RerunPlan(skipped="unsafe_id")
    return RerunPlan(tuple(failed_tests), tuple(prefix), attempts)


def rerun_count(patch: Mapping[str, Any]) -> int:
    """Reruns the patch allows: 0 when ``flaky_rerun`` is off, else ``flaky_reruns`` (capped)."""
    if not patch.get("flaky_rerun", True):
        return 0
    value = patch.get("flaky_reruns", DEFAULT_FLAKY_RERUNS)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_FLAKY_RERUNS
    return int(max(0, min(value, MAX_FLAKY_RERUNS)))


def rerun_candidates(
    patch: Mapping[str, Any],
    test_commands: list[str],
    passed: bool,
    failed_tests: list[str],
) -> list[str]:
    """The failed node ids :func:`plan_rerun` would rerun (``[]`` for none)."""
    return list(plan_rerun(patch, test_commands, passed, failed_tests).ids)


def pytest_prefix(command: str) -> list[str] | None:
    """The words of *command* up to and including pytest, or ``None``.

    ``"python -m pytest -v"`` → ``["python", "-m", "pytest"]``,
    ``"pytest -x tests/"`` → ``["pytest"]``.  ``None`` when the command does
    not call pytest or its leading words are more than plain program names
    and options (environment assignments, shell syntax), so a rerun could
    not start pytest the same way.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for index, word in enumerate(words):
        if _PYTEST_WORD.search(word):
            prefix = words[: index + 1]
            return prefix if all(_RERUN_PREFIX_WORD.match(w) for w in prefix) else None
    return None


def _rerunnable(nodeid: str) -> bool:
    path = nodeid.split("::", 1)[0]
    return bool(_RERUNNABLE_ID.match(nodeid)) and ".." not in path.split("/")


def pytest_rerun_args(
    node_ids: Collection[str],
    prefix: Collection[str] = ("python", "-m", "pytest"),
) -> list[str]:
    """Return the argv that reruns *node_ids* once, quietly and without cache writes.

    *prefix* is how the test command started pytest (see :func:`pytest_prefix`),
    so the rerun imports the same way the run did.
    """
    return [*prefix, "-q", "-p", "no:cacheprovider", *node_ids]


def rerun_outcome(
    node_ids: Collection[str], returncode: int | None, output: str
) -> tuple[list[str], list[str]] | None:
    """``(passed, still_failing)`` of a rerun of *node_ids*, or ``None`` if it proves nothing.

    pytest exits 0 when every selected test passed and 1 when some failed.
    Any other result (usage error, nothing collected, timeout) or a failed
    run whose failures cannot be parsed proves nothing.
    """
    if returncode == 0:
        return list(node_ids), []
    still_failing = set(failed_test_ids(output)) if returncode == 1 else set()
    if not still_failing:
        return None
    return (
        [nodeid for nodeid in node_ids if nodeid not in still_failing],
        [nodeid for nodeid in node_ids if nodeid in still_failing],
    )


def flaky_from_rerun(node_ids: list[str], returncode: int | None, output: str) -> list[str]:
    """Return the *node_ids* that passed when rerun (``[]`` when the rerun proves nothing)."""
    outcome = rerun_outcome(node_ids, returncode, output)
    return outcome[0] if outcome else []


def flaky_tolerated(
    patch: Mapping[str, Any],
    failed_tests: list[str],
    flaky_tests: list[str],
    *,
    outcomes: Mapping[str, str] | None = None,
    failures: int | None = None,
) -> bool:
    """Whether a failed run counts as passed because every failure was a pre-existing flake.

    Only on opt-in (``tolerate_preexisting_flaky``, or the older
    ``tolerate_flaky``) and only when every failed id passed on a rerun,
    none of them is an ERROR (setup, teardown or collection; from
    *outcomes*), the run's failure count matches the failed ids, and no
    failed test lives in a file this run wrote.  Run ownership comes from
    the patch's ``run_files`` (every path the run wrote or deleted so far)
    plus its own files and deletions; without ``run_files`` it is unknown
    and nothing is tolerated.
    """
    if not (patch.get("tolerate_preexisting_flaky") or patch.get("tolerate_flaky")):
        return False
    if not failed_tests or not set(failed_tests) <= set(flaky_tests):
        return False
    if outcomes and any(outcomes.get(nodeid) == "error" for nodeid in failed_tests):
        return False
    if failures is not None and failures != len(failed_tests):
        return False
    owned = run_owned_files(patch)
    if owned is None:
        return False
    return not any(normalize_rel_path(nodeid.split("::", 1)[0]) in owned for nodeid in failed_tests)


def run_owned_files(patch: Mapping[str, Any]) -> set[str] | None:
    """Normalized paths this run wrote or deleted, or ``None`` when the patch does not say."""
    if "run_files" not in patch:
        return None
    paths = [
        *(patch.get("run_files") or []),
        *(patch.get("files") or {}),
        *(patch.get("delete_files") or []),
    ]
    return {p for p in map(normalize_rel_path, paths) if p}


def mark_flaky_tolerated(report: dict[str, Any]) -> None:
    """Count a failed run as passed: ``raw_passed`` keeps the real result."""
    report["raw_passed"] = False
    report["passed"] = True
    report["passed_with_flaky"] = True


# ---------------------------------------------------------------------------
# Entrypoint smoke runs
# ---------------------------------------------------------------------------


def entrypoint_paths(
    patch: Mapping[str, Any],
    root: Path,
    written: Collection[str] = (),
) -> list[str]:
    """Return the patch's ``entrypoints`` that are safe ``.py`` paths that exist.

    A path exists when it is a file under *root* or one of *written* (files
    staged somewhere else, e.g. in a remote sandbox).
    """
    staged = {p for p in map(normalize_rel_path, written) if p}
    paths: list[str] = []
    for path in patch.get("entrypoints") or []:
        if not (
            isinstance(path, str) and _ENTRYPOINT_PATH.match(path) and ".." not in path.split("/")
        ):
            logger.warning("Skipping entrypoint that is not a safe relative .py path: %r", path)
        elif path not in paths and (path in staged or (root / path).is_file()):
            paths.append(path)
    return paths


def entrypoint_run(
    file: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool = False,
) -> EntrypointRun:
    """Build an :class:`EntrypointRun`, keeping the head of stdout and the tail of stderr."""
    return EntrypointRun(
        file=file,
        returncode=None if timed_out else returncode,
        timed_out=timed_out,
        stdout_head=stdout[:_ENTRYPOINT_OUTPUT_CHARS],
        stderr_tail=stderr[-_ENTRYPOINT_OUTPUT_CHARS:],
    )


class BaseSandbox(ABC):
    """Base interface for code execution sandboxes.

    Sandboxes apply patches, run tests, and return structured TestReports.
    Docker and E2B sandboxes isolate execution from the host; the local
    sandbox does not and runs code directly on the host.
    """

    @abstractmethod
    async def apply_and_test(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a PatchSet and run tests, returning a TestReport."""
        ...

    def cleanup(self) -> None:  # noqa: B027
        """Release resources the sandbox created (images, volumes, containers).

        Safe to call more than once; the orchestrator calls it when a run ends.
        """
