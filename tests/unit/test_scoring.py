"""Tests for EMCS scoring."""

import pytest

from delegatron.orchestrator.scoring import (
    can_override_rejection,
    classify_test_evidence,
    compute_score,
    get_score_summary,
    output_shows_tests_ran,
    should_accept,
)

# --- compute_score tests (unchanged behavior) ---


def test_perfect_score_no_issues():
    review = {"issues": []}
    assert compute_score(review) == 1.0


def test_score_with_minor_issues():
    review = {"issues": [{"severity": "minor"}, {"severity": "nit"}]}
    score = compute_score(review)
    # minor=1.0, nit=0.2, total=1.2/20 = 0.06 deduction -> 0.94
    assert 0.9 < score < 1.0


def test_score_with_blocker():
    review = {"issues": [{"severity": "blocker"}]}
    score = compute_score(review)
    # blocker=10.0/20 = 0.5 deduction -> 0.5
    assert score == 0.5


def test_test_pass_bonus():
    review = {"issues": [{"severity": "major"}]}
    test_report = {"passed": True, "total": 3}
    score_with_tests = compute_score(review, test_report)
    score_without = compute_score(review)
    assert score_with_tests > score_without


# --- should_accept: hard gates ---


def test_should_accept_no_issues_tests_pass():
    """Clean review + passing tests + patch applied = accepted."""
    review = {"issues": []}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is True


def test_should_accept_minor_only_tests_pass():
    """Minor issues only + passing tests + patch applied = accepted."""
    review = {"issues": [{"severity": "minor"}]}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is True


def test_should_accept_blocked_by_failing_tests():
    """Failing tests always block, even with clean review."""
    review = {"issues": []}
    test_report = {"passed": False, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is False


def test_should_accept_blocked_by_no_test_report():
    """Missing test report always blocks (tests never ran)."""
    review = {"issues": []}
    assert should_accept(review, test_report=None, patch_applied=True) is False


def test_should_accept_blocked_by_patch_failure():
    """Patch not applied cleanly always blocks."""
    review = {"issues": []}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=False) is False


def test_should_accept_blocker_always_blocks():
    """Blocker issues block even with passing tests and clean patch."""
    review = {"issues": [{"severity": "blocker"}]}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is False


def test_should_accept_major_always_blocks():
    """Major issues block — EMCS is only a tie-breaker for minor/nit."""
    review = {"issues": [{"severity": "major"}]}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is False


def test_should_accept_many_majors():
    """Multiple major issues should definitely block."""
    review = {"issues": [{"severity": "major"}] * 5}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is False


def test_should_accept_nits_only_with_tests():
    """Nit issues only + passing tests = accepted (EMCS tie-break)."""
    review = {"issues": [{"severity": "nit"}, {"severity": "nit"}]}
    test_report = {"passed": True, "total": 3}
    assert should_accept(review, test_report, patch_applied=True) is True


# --- Backward compat: default patch_applied=True for callers that don't set it ---


def test_should_accept_default_patch_applied():
    """When patch_applied is not passed, defaults to True (backward compat)."""
    review = {"issues": []}
    test_report = {"passed": True, "total": 3}
    # Call without patch_applied kwarg
    assert should_accept(review, test_report) is True


# --- get_score_summary ---


def test_score_summary():
    review = {
        "issues": [
            {"severity": "major"},
            {"severity": "minor"},
            {"severity": "minor"},
        ]
    }
    summary = get_score_summary(review)
    assert "score" in summary
    assert summary["issue_counts"]["major"] == 1
    assert summary["issue_counts"]["minor"] == 2
    assert "acceptable" in summary


# --- Test evidence (#102): skipped, synthetic-only and zero-test runs prove nothing ---

SKIPPED = {"passed": False, "total": 0, "skipped_reason": "non_testable_language"}
SYNTHETIC = {"passed": True, "total": 2, "synthetic_tests": True}
ZERO_TESTS = {"passed": True, "total": 0}
REAL_PASS = {"passed": True, "total": 3}


@pytest.mark.parametrize(
    ("report", "evidence"),
    [
        (SKIPPED, "untested"),
        ({**SKIPPED, "passed": True}, "untested"),
        (SYNTHETIC, "untested"),
        (ZERO_TESTS, "untested"),
        (REAL_PASS, "passed"),
        ({"passed": False, "total": 3}, "failed"),
        ({"passed": False, "total": 2, "synthetic_tests": True}, "failed"),
        (None, "failed"),
        # #104: only failures a skipped task left behind
        ({"passed": False, "total": 4, "task_passed": True}, "passed"),
        ({"passed": False, "total": 4, "task_passed": False}, "failed"),
    ],
)
def test_classify_test_evidence(report, evidence):
    assert classify_test_evidence(report) == evidence


def test_untested_needs_explicit_permission():
    review = {"issues": [{"severity": "minor"}]}
    assert should_accept(review, SKIPPED, patch_applied=True) is False
    assert should_accept(review, SKIPPED, patch_applied=True, allow_untested=True) is True
    assert should_accept({"issues": [{"severity": "major"}]}, SKIPPED, allow_untested=True) is False
    assert should_accept({"issues": []}, None, allow_untested=True) is False


@pytest.mark.parametrize("report", [SKIPPED, SYNTHETIC, ZERO_TESTS])
def test_no_test_bonus_without_real_tests(report):
    review = {"issues": [{"severity": "major"}]}
    assert compute_score(review, report) == compute_score(review) == 0.75


def test_task_passed_counts_as_passing():
    report = {"passed": False, "total": 4, "task_passed": True}
    assert should_accept({"issues": []}, report, patch_applied=True) is True


# --- EMCS override (#103) ---

MINORS = {"acceptance_met": False, "issues": [{"severity": "minor"}, {"severity": "nit"}]}


@pytest.mark.parametrize(
    ("review", "report", "applied"),
    [
        ({"acceptance_met": False, "summary": "wrong", "issues": []}, REAL_PASS, True),
        ({"issues": [{"severity": "minor"}, {"severity": "major"}]}, REAL_PASS, True),
        ({"issues": [{"severity": "blocker"}]}, REAL_PASS, True),
        (MINORS, SKIPPED, True),
        (MINORS, SYNTHETIC, True),
        (MINORS, REAL_PASS, False),
        (MINORS, {"passed": False, "total": 3}, True),
    ],
)
def test_rejection_not_overridable(review, report, applied):
    assert can_override_rejection(review, report, threshold=0.6, patch_applied=applied) is False


def test_minor_only_rejection_with_real_passing_tests_is_overridable():
    assert can_override_rejection(MINORS, REAL_PASS, threshold=0.6, patch_applied=True) is True


def test_score_summary_reports_the_configured_threshold():
    twelve_minor = {"acceptance_met": False, "issues": [{"severity": "minor"}] * 12}

    summary = get_score_summary(twelve_minor, REAL_PASS, patch_applied=True, threshold=0.6)

    assert summary["threshold"] == 0.6
    assert summary["acceptable"] is True
    default = get_score_summary(twelve_minor, REAL_PASS, patch_applied=True)
    assert default["threshold"] == 0.8
    assert default["acceptable"] is False


# --- passing runs whose count the sandbox did not parse (D9) ---

UNPARSED_PASSES = {
    "dotnet": "Passed!  - Failed:     0, Passed:    12, Skipped:     0, Total:    12, Duration: 2s",
    "dotnet 8": "Test summary: total: 12, failed: 0, succeeded: 12, skipped: 0, duration: 1.2s",
    "dotnet legacy": "Total tests: 12\n     Passed: 12",
    "pester": "Tests completed in 1.2s\nTests Passed: 5, Failed: 0, Skipped: 0 NotRun: 0",
    "ctest": "100% tests passed, 0 tests failed out of 3",
    "rspec": "Finished in 0.01 seconds\n4 examples, 0 failures",
    "mocha": "  3 passing (12ms)",
    "node:test": "# tests 3\n# pass 3\n# fail 0",
    "django": "Creating test database...\nRan 7 tests in 0.512s\n\nOK",
    "maven": "Tests run: 9, Failures: 0, Errors: 0, Skipped: 0",
    "phpunit": "OK (6 tests, 11 assertions)",
    "go": "ok  \texample.com/deck\t0.012s\n?   \texample.com/cmd\t[no test files]",
    "go -v": "=== RUN   TestDeal\n--- PASS: TestDeal (0.00s)\nPASS",
    "gradle": "> Task :compileJava\n> Task :test\n\nBUILD SUCCESSFUL in 3s",
    "cargo": "test result: ok. 4 passed; 0 failed; 0 ignored",
}

ZERO_TEST_PASSES = {
    "pytest": "collected 0 items\n\n============ no tests ran in 0.01s ============",
    "unittest": "Ran 0 tests in 0.000s\n\nOK",
    "go": "?   \texample.com/deck\t[no test files]",
    "go filtered": "ok  \texample.com/deck\t0.002s [no tests to run]",
    "jest": "No tests found, exiting with code 0",
    "mocha": "  0 passing (1ms)",
    "rspec": "0 examples, 0 failures",
    "gradle": "> Task :test NO-SOURCE\n\nBUILD SUCCESSFUL in 1s",
    "silent": "",
}


@pytest.mark.parametrize("runner", sorted(UNPARSED_PASSES))
def test_passing_run_with_an_unparsed_count_is_tested(runner):
    report = {"passed": True, "total": 0, "stdout": UNPARSED_PASSES[runner]}
    assert output_shows_tests_ran(report) is True
    assert classify_test_evidence(report) == "passed"
    assert classify_test_evidence({**report, "total": None}) == "passed"


@pytest.mark.parametrize("runner", sorted(ZERO_TEST_PASSES))
def test_passing_run_that_ran_no_test_is_untested(runner):
    report = {"passed": True, "total": 0, "stdout": ZERO_TEST_PASSES[runner]}
    assert classify_test_evidence(report) == "untested"


def test_unparsed_count_does_not_rescue_skipped_synthetic_or_failed_runs():
    output = UNPARSED_PASSES["dotnet"]
    assert classify_test_evidence({**SKIPPED, "stdout": output}) == "untested"
    assert classify_test_evidence({**SYNTHETIC, "total": 0, "stdout": output}) == "untested"
    assert classify_test_evidence({"passed": False, "total": 0, "stdout": output}) == "failed"


def test_minor_review_of_an_unparsed_passing_run_is_accepted():
    """The 05f8c7c behaviour: 4 minor + 1 nit with tests that ran is acceptable (0.8)."""
    review = {"issues": [{"severity": "minor"}] * 4 + [{"severity": "nit"}]}
    report = {"passed": True, "total": 0, "stdout": UNPARSED_PASSES["dotnet"]}
    assert should_accept(review, report, patch_applied=True, allow_untested=True) is True
    assert can_override_rejection(review, report, patch_applied=True) is True


def test_score_summary_uses_the_callers_untested_permission():
    review = {"acceptance_met": True, "issues": []}
    summary = get_score_summary(review, SKIPPED, patch_applied=True)
    assert summary["acceptable"] is False
    assert summary["test_evidence"] == "untested"
    allowed = get_score_summary(review, SKIPPED, patch_applied=True, allow_untested=True)
    assert allowed["acceptable"] is True
