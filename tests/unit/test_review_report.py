"""Tests for ReviewReport parsing: fix hints and lenient repair of model slips."""

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from delegatron.schemas.review_report import ReviewIssue, ReviewReport

_ISSUE = {
    "severity": "major",
    "title": "Deal size wrong",
    "details": "assert 12 == 10",
    "recommended_fix": "Deal 10 cards in deal.py",
}


def test_fix_hints_default_to_none():
    issue = ReviewIssue(**_ISSUE)

    assert issue.fix_target is None
    assert issue.fix_file is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("code", "code"),
        ("test", "test"),
        ("architecture", "architecture"),
        (" Test ", "test"),
        ("CODE", "code"),
        ("both", None),
        ("", None),
        (None, None),
        (1, None),
    ],
)
def test_fix_target_is_coerced(raw, expected):
    assert ReviewIssue(**_ISSUE, fix_target=raw).fix_target == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tests/test_deal.py", "tests/test_deal.py"),
        ("  deal.py ", "deal.py"),
        ("   ", None),
        (None, None),
        (["deal.py"], None),
    ],
)
def test_fix_file_is_coerced(raw, expected):
    assert ReviewIssue(**_ISSUE, fix_file=raw).fix_file == expected


def test_review_report_parses_model_output_with_hints():
    report = ReviewReport.model_validate(
        {
            "summary": "Flaky shuffle test",
            "acceptance_met": False,
            "issues": [{**_ISSUE, "fix_target": "Tests", "fix_file": "tests/test_deck.py"}],
        }
    )

    assert report.issues[0].fix_target is None
    assert report.issues[0].fix_file == "tests/test_deck.py"


def test_review_report_json_schema_matches_model():
    """schemas/review_report.schema.json lists every ReviewIssue field."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "review_report.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    issue = schema["properties"]["issues"]["items"]

    assert set(issue["properties"]) == set(ReviewIssue.model_fields)
    assert issue["properties"]["fix_target"]["enum"] == ["code", "test", "architecture", None]


def test_lenient_validation_keeps_the_model_facing_schema():
    """Repairs happen before validation; the schema sent to models is unchanged."""
    path = Path(__file__).resolve().parents[2] / "schemas" / "review_report.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    generated = ReviewReport.model_json_schema()
    issue = generated["$defs"]["ReviewIssue"]

    assert generated["required"] == schema["required"]
    assert issue["required"] == schema["properties"]["issues"]["items"]["required"]
    assert issue["properties"]["severity"]["enum"] == ["blocker", "major", "minor", "nit"]


# ---------------------------------------------------------------------------
# Lenient parsing of reviewer slips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("major", "major"),
        ("Major", "major"),
        (" NIT ", "nit"),
        ("CRITICAL", "blocker"),
        ("fatal", "blocker"),
        ("high", "major"),
        ("serious", "major"),
        ("warning", "minor"),
        ("medium", "minor"),
        ("info", "nit"),
        ("style", "nit"),
        ("bogus", "major"),
        ("", "major"),
        (None, "major"),
        (3, "major"),
    ],
)
def test_severity_is_coerced_and_fails_closed(raw, expected):
    assert ReviewIssue(**{**_ISSUE, "severity": raw}).severity == expected


def test_coerced_severity_is_logged(caplog):
    logger = logging.getLogger("delegatron.schemas.review_report")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            ReviewIssue(**{**_ISSUE, "severity": "critical"})
    finally:
        logger.removeHandler(caplog.handler)

    assert "'critical'" in caplog.text
    assert "'blocker'" in caplog.text


def test_missing_text_fields_become_empty():
    issue = ReviewIssue.model_validate({"severity": "minor", "title": "Typo"})

    assert (issue.details, issue.recommended_fix, issue.evidence) == ("", "", "")


def test_missing_severity_fails_closed():
    assert ReviewIssue.model_validate({"title": "Typo"}).severity == "major"


def test_null_and_list_text_fields_are_repaired():
    issue = ReviewIssue.model_validate(
        {**_ISSUE, "details": None, "evidence": ["deal.py:3", "deal.py:9"]}
    )

    assert issue.details == ""
    assert issue.evidence == "deal.py:3\ndeal.py:9"


def test_dict_text_field_is_still_rejected():
    with pytest.raises(ValidationError):
        ReviewIssue.model_validate({**_ISSUE, "details": {"why": "x"}})


def test_missing_title_is_still_rejected():
    with pytest.raises(ValidationError):
        ReviewIssue.model_validate({"severity": "major", "details": "x"})


def test_null_issues_means_no_issues():
    report = ReviewReport.model_validate({"summary": "ok", "acceptance_met": True, "issues": None})

    assert report.issues == []


def test_reviewer_slips_from_issue_108_validate():
    report = ReviewReport.model_validate(
        {
            "summary": "...",
            "acceptance_met": False,
            "issues": [{"severity": "critical", "title": "SQL injection", "details": "..."}],
        }
    )

    assert report.issues[0].severity == "blocker"
    assert report.issues[0].recommended_fix == ""
