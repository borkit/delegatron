"""TestReport schema — sandbox runner output."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

# Why a failed run's failing tests were not rerun for flakiness (``rerun_skipped``).
RERUN_SKIPPED_REASONS = (
    "disabled",
    "no_ids",
    "not_pytest",
    "too_many",
    "unsafe_id",
    "windows_image",
)


class SandboxTestResult(BaseModel):
    """Result of a single test."""

    name: str
    passed: bool
    # pytest outcome: passed, failed, error, skipped, xfailed or xpassed ("" = unknown)
    status: str = ""
    duration_ms: float = 0.0
    output: str = ""
    error: str = ""  # first error line of a failing test


# Keep backward-compatible alias
TestResult = SandboxTestResult


class EntrypointRun(BaseModel):
    """Smoke run of a script output (``python <file>``, empty stdin, short timeout)."""

    file: str
    returncode: int | None = None
    timed_out: bool = False
    stdout_head: str = ""  # first 2000 chars
    stderr_tail: str = ""  # last 2000 chars


class TargetedRun(BaseModel):
    """The patch's ``targeted_tests`` (its own test commands), run before the full suite."""

    passed: bool
    failing_ids: list[str] = []
    stdout_tail: str = ""  # last 2000 chars of the combined output


class SandboxReport(BaseModel):
    """Structured output from the sandbox test runner.

    Contains pass/fail status, individual test results,
    and captured stdout/stderr.  Per-test fields are computed from the
    full output before ``stdout``/``stderr`` are truncated;
    ``output_truncated`` says whether that cut anything.
    """

    __test__: ClassVar[bool] = False  # not a pytest test class

    passed: bool
    total: int
    failures: int
    results: list[SandboxTestResult] = []  # one per pytest node id
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    failed_tests: list[str] = []  # node ids that failed or errored
    failure_excerpt: str = ""  # failure tracebacks + short summary
    flaky_tests: list[str] = []  # failed, then passed on rerun
    entrypoint_runs: list[EntrypointRun] = []
    targeted: TargetedRun | None = None
    lint_warnings: list[str] = []  # non-blocking parse-gate advisories
    output_truncated: bool = False  # stdout or stderr was cut to its size limit
    rerun_attempts: int = 0  # flakiness reruns of the failing tests
    rerun_skipped: str = ""  # one of RERUN_SKIPPED_REASONS when a failed run was not rerun
    skipped_reason: str = ""  # why no test ran (e.g. "non_testable_language")
    synthetic_tests: bool = False  # only the orchestrator's smoke tests ran
    # tolerated pre-existing flakes: ``passed`` is True, ``raw_passed`` the real result
    raw_passed: bool | None = None
    passed_with_flaky: bool = False


# Keep backward-compatible alias (``__test__`` keeps pytest from collecting it)
TestReport = SandboxReport
