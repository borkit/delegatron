"""Evaluation Matrix Consensus Scoring (EMCS).

Combines reliability metrics (confidence, inter-agent agreement) with
impact metrics (severity, category) to produce a numeric quality score
for the current iteration. Used to decide whether the code is "good enough"
even if the reviewer found minor issues.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Severity weights for scoring
SEVERITY_WEIGHTS = {
    "blocker": 10.0,
    "major": 5.0,
    "minor": 1.0,
    "nit": 0.2,
}

# Threshold above which the code is considered acceptable
ACCEPTANCE_THRESHOLD = 0.8

# Bonus for passing tests
TEST_PASS_BONUS = 0.3

# Severities an EMCS override may accept over a reviewer rejection
_OVERRIDABLE_SEVERITIES = frozenset({"minor", "nit"})

TestEvidence = Literal["passed", "failed", "untested"]

# Test counts runners print, by runner: pytest/cargo/jest ("3 passed"),
# unittest/Django ("Ran 3 tests"), JUnit via Maven/Gradle ("Tests run: 3"),
# rspec ("3 examples, 0 failures"), mocha ("3 passing"), node:test/TAP
# ("# tests 3", "1..3"), ctest ("... failed out of 3"), dotnet ("Passed: 3",
# "Total tests: 3", "Test summary: total: 3"), Pester ("Tests Passed: 3"),
# PHPUnit ("OK (3 tests").
_TEST_COUNTS = re.compile(
    r"""\b(\d+)\ (?:passed|failed|passing)\b
      | ^Ran\ (\d+)\ tests?\b
      | \bTests\ run:\ *(\d+)
      | \b(\d+)\ examples?,\ \d+\ failures?
      | ^\#\ tests\ (\d+)\s*$
      | ^1\.\.(\d+)\s*$
      | \bfailed\ out\ of\ (\d+)
      | \b(?:Passed|Failed|Total|Total\ tests|Test\ summary:\ total):\ *(\d+)
      | \bTests\ Passed:\ *(\d+)
      | ^OK\ \((\d+)\ tests?""",
    re.MULTILINE | re.VERBOSE,
)

# Lines that show tests ran without a count: go ("ok  pkg 0.01s", "--- PASS:",
# "PASS"), unittest ("OK"), Gradle ("> Task :test").
_TESTS_RAN = re.compile(
    r"^(?:ok\s+\S+|---\ PASS:|PASS\s*$|OK(?:\ \(.*\))?\s*$|>\ Task\ :(?:\S+:)?test\s*$)",
    re.MULTILINE | re.VERBOSE,
)
# ...unless the same line says nothing was tested.
_NOTHING_RAN = re.compile(r"no test files|no tests to run|NO-SOURCE", re.IGNORECASE)


def classify_test_evidence(test_report: dict[str, Any] | None) -> TestEvidence:
    """Classify what a sandbox report proves about the code.

    ``untested`` when no real test exercised it: the sandbox was skipped
    (``skipped_reason``), only the orchestrator's synthetic smoke tests ran
    (``synthetic_tests``), or a passing run collected no test.  A passing run
    whose count the sandbox could not parse (``total`` 0 or missing) counts
    as tested when its output shows tests ran (``output_shows_tests_ran``).
    ``passed`` for a passing run (``task_passed`` — no failure beyond the
    known ones — counts as passing).  ``failed`` otherwise, including a
    missing report.
    """
    if not test_report:
        return "failed"
    if test_report.get("skipped_reason"):
        return "untested"
    if not test_report.get("task_passed", test_report.get("passed")):
        return "failed"
    if test_report.get("synthetic_tests"):
        return "untested"
    if not test_report.get("total") and not output_shows_tests_ran(test_report):
        return "untested"
    return "passed"


def output_shows_tests_ran(test_report: dict[str, Any]) -> bool:
    """Whether a run's output shows that at least one test ran.

    A test count any common runner prints decides (``3 passed``, ``Ran 3
    tests``, ``Tests run: 3``, ``Passed: 3`` …): some count above zero means
    tests ran, only zero counts mean none did.  Without any count, a runner's
    success line does (go ``ok  pkg``, ``--- PASS:``, unittest ``OK``, Gradle
    ``> Task :test``), except one that says nothing was tested (``[no test
    files]``, ``NO-SOURCE``).
    """
    output = f"{test_report.get('stdout') or ''}\n{test_report.get('stderr') or ''}"
    counts = [
        int(next(group for group in match.groups() if group is not None))
        for match in _TEST_COUNTS.finditer(output)
    ]
    if counts:
        return max(counts) > 0
    return any(
        _TESTS_RAN.match(line.strip()) and not _NOTHING_RAN.search(line)
        for line in output.splitlines()
    )


def compute_score(
    review: dict[str, Any],
    test_report: dict[str, Any] | None = None,
    max_possible_deductions: float = 20.0,
) -> float:
    """Compute a quality score from 0.0 to 1.0.

    Score starts at 1.0 and is reduced by issue severity.
    Passing tests provide a bonus that offsets minor issues; skipped,
    synthetic-only or zero-test runs earn no bonus.
    """
    issues = review.get("issues", [])

    # Calculate total deduction from issues
    total_deduction = 0.0
    for issue in issues:
        severity = issue.get("severity", "minor")
        weight = SEVERITY_WEIGHTS.get(severity, 1.0)
        total_deduction += weight

    # Normalize deductions against the max possible
    if max_possible_deductions <= 0:
        max_possible_deductions = 20.0
    deduction_ratio = min(total_deduction / max_possible_deductions, 1.0)
    score = 1.0 - deduction_ratio

    # Bonus for real passing tests
    tests_passed = classify_test_evidence(test_report) == "passed"
    if tests_passed:
        score = min(score + TEST_PASS_BONUS, 1.0)

    score = max(score, 0.0)

    logger.debug(
        "EMCS score: %.2f (deductions=%.1f, test_bonus=%s)",
        score,
        total_deduction,
        tests_passed,
    )

    return round(score, 3)


def should_accept(
    review: dict[str, Any],
    test_report: dict[str, Any] | None = None,
    threshold: float = ACCEPTANCE_THRESHOLD,
    patch_applied: bool = True,
    *,
    allow_untested: bool = False,
) -> bool:
    """Determine if the code quality is acceptable based on EMCS score.

    Returns True only when ALL of these conditions are met:
    1. Patch was successfully applied to the workspace
    2. Sandbox tests passed (or, with *allow_untested*, no test could run —
       for a reviewer that explicitly accepted untested code)
    3. No blocker-severity issues
    4. No major-severity issues
    5. EMCS score meets threshold

    EMCS acts as a tie-breaker for minor/nit issues only — it cannot
    override hard failures (broken patch, failed tests, blocker/major).
    """
    # Hard gate: patch must have applied successfully
    if not patch_applied:
        logger.debug("should_accept: blocked — patch did not apply")
        return False

    # Hard gate: tests must have passed
    evidence = classify_test_evidence(test_report)
    if evidence == "failed" or (evidence == "untested" and not allow_untested):
        logger.debug("should_accept: blocked — tests %s", evidence)
        return False

    # Hard gate: no blocker or major issues
    issues = review.get("issues", [])
    has_blocker = any(i.get("severity") == "blocker" for i in issues)
    has_major = any(i.get("severity") == "major" for i in issues)
    if has_blocker:
        logger.debug("should_accept: blocked — blocker issue present")
        return False
    if has_major:
        logger.debug("should_accept: blocked — major issue present")
        return False

    # Tie-breaker: EMCS score for minor/nit issues
    score = compute_score(review, test_report)
    return score >= threshold


def can_override_rejection(
    review: dict[str, Any],
    test_report: dict[str, Any] | None,
    *,
    threshold: float = ACCEPTANCE_THRESHOLD,
    patch_applied: bool,
) -> bool:
    """Whether EMCS may approve a patch the reviewer rejected.

    Only a rejection that names its reasons, all of them minor or nit, can be
    overridden, and only with real passing tests (no skipped, synthetic-only or
    zero-test run, where the reviewer is the only gate); then ``should_accept``
    decides.
    """
    issues = review.get("issues") or []
    if not issues or any(i.get("severity") not in _OVERRIDABLE_SEVERITIES for i in issues):
        return False
    if classify_test_evidence(test_report) != "passed":
        return False
    return should_accept(review, test_report, threshold=threshold, patch_applied=patch_applied)


def get_score_summary(
    review: dict[str, Any],
    test_report: dict[str, Any] | None = None,
    patch_applied: bool = False,
    threshold: float = ACCEPTANCE_THRESHOLD,
    *,
    allow_untested: bool = False,
) -> dict[str, Any]:
    """Return a detailed score breakdown for logging/display.

    ``acceptable`` is ``should_accept`` with the same *allow_untested* the
    caller's gate uses; ``test_evidence`` is ``classify_test_evidence``.
    """
    issues = review.get("issues", [])

    severity_counts: dict[str, int] = {}
    severity_deductions: dict[str, float] = {}
    for issue in issues:
        sev = issue.get("severity", "minor")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
        weight = SEVERITY_WEIGHTS.get(sev, 1.0)
        severity_deductions[sev] = severity_deductions.get(sev, 0.0) + weight

    score = compute_score(review, test_report)
    evidence = classify_test_evidence(test_report)

    return {
        "score": score,
        "threshold": threshold,
        "acceptable": should_accept(
            review,
            test_report,
            threshold=threshold,
            patch_applied=patch_applied,
            allow_untested=allow_untested,
        ),
        "issue_counts": severity_counts,
        "deductions": severity_deductions,
        "tests_passed": evidence == "passed",
        "test_evidence": evidence,
    }
