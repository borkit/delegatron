"""Tests for Pydantic schema models."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from delegatron.schemas import PatchSet, ReviewReport, TestReport, WorkPlan
from delegatron.schemas.test_report import (
    RERUN_SKIPPED_REASONS,
    EntrypointRun,
    SandboxTestResult,
    TargetedRun,
)


def test_review_report_with_issues():
    """Verify ReviewReport parses nested issue objects correctly."""
    report = ReviewReport(
        summary="Found issues",
        acceptance_met=False,
        issues=[
            {
                "severity": "blocker",
                "title": "SQL injection",
                "details": "User input not sanitized",
                "recommended_fix": "Use parameterized queries",
            }
        ],
    )
    assert len(report.issues) == 1
    assert report.issues[0].severity == "blocker"


def test_patch_set_blocked_with_questions():
    """Verify PatchSet handles blocked status with questions."""
    ps = PatchSet(
        status="blocked",
        diff_unified="",
        rationale="Need clarification",
        tests_to_run=[],
        questions=["Which database?"],
    )
    assert ps.status == "blocked"
    assert len(ps.questions) == 1


def test_test_report_without_new_fields_still_validates():
    """Reports saved before per-test fields existed load with empty defaults."""
    report = TestReport.model_validate(
        {"passed": False, "total": 7, "failures": 1, "results": [], "stdout": "...", "stderr": ""}
    )
    assert report.failed_tests == []
    assert report.failure_excerpt == ""
    assert report.flaky_tests == []
    assert report.entrypoint_runs == []


def test_test_report_per_test_fields_round_trip():
    """Per-test results, flaky ids and entrypoint runs survive a dump/validate cycle."""
    nodeid = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
    report = TestReport(
        passed=False,
        total=2,
        failures=1,
        results=[{"name": nodeid, "passed": False}, {"name": "tests/test_a.py::t", "passed": True}],
        failed_tests=[nodeid],
        failure_excerpt="E           AssertionError: 12 != 10",
        flaky_tests=["tests/test_card_deck.py::TestDeck::test_shuffle"],
        entrypoint_runs=[
            {"file": "simulate_deal.py", "returncode": 0, "stdout_head": "Player 1: ..."},
            {"file": "loop.py", "timed_out": True},
        ],
    )
    restored = TestReport.model_validate(report.model_dump())
    assert restored == report
    assert restored.entrypoint_runs[1].returncode is None
    assert restored.entrypoint_runs[1].timed_out is True
    assert restored.entrypoint_runs[1].stderr_tail == ""


def test_test_report_json_schema_matches_model():
    """schemas/test_report.schema.json lists every SandboxReport and EntrypointRun field."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "test_report.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert set(schema["properties"]) == set(TestReport.model_fields)
    entrypoint = schema["properties"]["entrypoint_runs"]["items"]
    assert set(entrypoint["properties"]) == set(EntrypointRun.model_fields)
    assert entrypoint["required"] == ["file"]
    targeted = schema["properties"]["targeted"]
    assert set(targeted["properties"]) == set(TargetedRun.model_fields)


def test_test_report_truncation_rerun_and_skip_fields_default():
    """#69/#77/#102 fields: defaulted, so stored reports still validate."""
    old = TestReport.model_validate(
        {"passed": False, "total": 7, "failures": 1, "results": [{"name": "t", "passed": False}]}
    )
    assert old.output_truncated is False
    assert old.rerun_attempts == 0
    assert old.rerun_skipped == ""
    assert old.skipped_reason == ""
    assert old.synthetic_tests is False
    assert old.results[0].status == ""
    report = TestReport(
        passed=False,
        total=12,
        failures=11,
        results=[SandboxTestResult(name="t", passed=False, status="error", error="E")],
        output_truncated=True,
        rerun_skipped="too_many",
        skipped_reason="non_testable_language",
        synthetic_tests=True,
    )
    assert TestReport.model_validate(report.model_dump()) == report


def test_test_report_json_schema_lists_new_fields():
    path = Path(__file__).resolve().parents[2] / "schemas" / "test_report.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    result = schema["properties"]["results"]["items"]["properties"]
    assert set(result) == set(SandboxTestResult.model_fields)
    rerun_skipped = schema["properties"]["rerun_skipped"]
    assert rerun_skipped["enum"] == ["", *RERUN_SKIPPED_REASONS]
    for name in ("output_truncated", "rerun_attempts", "skipped_reason", "synthetic_tests"):
        assert "default" in schema["properties"][name]
        assert name not in schema["required"]


def test_test_report_is_not_collected_by_pytest():
    assert TestReport.__test__ is False


def test_targeted_run_and_lint_warnings_round_trip():
    """Sandboxes report ``targeted`` and ``lint_warnings``; older reports have neither."""
    old = TestReport.model_validate({"passed": True, "total": 1, "failures": 0})
    assert old.targeted is None
    assert old.lint_warnings == []
    report = TestReport.model_validate(
        {
            "passed": False,
            "total": 2,
            "failures": 1,
            "targeted": {"passed": False, "failing_ids": ["tests/test_a.py::test_x"]},
            "lint_warnings": ["[lint] a.ps1: [PSAvoidUsingWriteHost] ..."],
        }
    )
    assert report.targeted is not None
    assert report.targeted.failing_ids == ["tests/test_a.py::test_x"]
    assert TestReport.model_validate(report.model_dump()) == report


# ---------------------------------------------------------------------------
# WorkPlan: lenient parsing of architect slips (#108)
# ---------------------------------------------------------------------------

_TASK = {"task_id": "t1", "title": "Deck", "description": "Build a deck", "outputs": ["deck.py"]}


def _plan(**overrides):
    plan = {
        "summary": "Deal cards",
        "tasks": [dict(_TASK, risk_level="low")],
        "acceptance": {"criteria": ["Deals 7 cards"], "tests": ["pytest"]},
        "termination": {"done_when": ["tests pass"]},
    }
    plan.update(overrides)
    return plan


def test_work_plan_without_termination_validates():
    plan = WorkPlan.model_validate({k: v for k, v in _plan().items() if k != "termination"})
    assert plan.termination.done_when == []


def test_work_plan_without_done_when_validates():
    assert WorkPlan.model_validate(_plan(termination={})).termination.done_when == []


def test_task_without_risk_level_defaults_to_medium():
    assert WorkPlan.model_validate(_plan(tasks=[_TASK])).tasks[0].risk_level == "medium"


def test_task_ids_and_dependencies_become_strings():
    tasks = [dict(_TASK, task_id=1), dict(_TASK, task_id=2, depends_on=[1])]
    plan = WorkPlan.model_validate(_plan(tasks=tasks))
    assert [t.task_id for t in plan.tasks] == ["1", "2"]
    assert plan.tasks[1].depends_on == ["1"]


def test_null_lists_become_empty():
    plan = WorkPlan.model_validate(
        _plan(tasks=[dict(_TASK, depends_on=None)], assumptions=None, open_questions=None)
    )
    assert plan.tasks[0].depends_on == []
    assert plan.assumptions == []
    assert plan.open_questions == []


def test_acceptance_tests_missing_or_a_string():
    missing = WorkPlan.model_validate(_plan(acceptance={"criteria": ["Deals 7 cards"]}))
    single = WorkPlan.model_validate(_plan(acceptance={"criteria": "Deals", "tests": "pytest"}))
    assert missing.acceptance.tests == []
    assert single.acceptance.criteria == ["Deals"]
    assert single.acceptance.tests == ["pytest"]


def test_work_plan_without_tasks_is_still_rejected():
    with pytest.raises(ValidationError):
        WorkPlan.model_validate({k: v for k, v in _plan().items() if k != "tasks"})


def test_work_plan_json_schema_keeps_required_fields():
    """The repairs run before validation: the schema shown to models still requires them."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "work_plan.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    generated = WorkPlan.model_json_schema()
    defs = generated["$defs"]
    props = schema["properties"]

    assert generated["required"] == schema["required"]
    assert defs["Task"]["required"] == props["tasks"]["items"]["required"]
    assert defs["AcceptanceCriteria"]["required"] == props["acceptance"]["required"]
    assert defs["TerminationPolicy"]["required"] == props["termination"]["required"]


def test_package_exports_the_director_and_edit_models():
    """#74/#98: ArbitrationReport and FileEdit are importable from delegatron.schemas."""
    import delegatron.schemas as schemas
    from delegatron.schemas.arbitration import ArbitrationReport
    from delegatron.schemas.patch_set import FileEdit

    assert schemas.ArbitrationReport is ArbitrationReport
    assert schemas.FileEdit is FileEdit
    assert {"ArbitrationReport", "FileEdit", "Resolution"} <= set(schemas.__all__)
