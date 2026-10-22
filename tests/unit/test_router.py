"""Tests for routing logic."""

from typing import Any

import pytest

from delegatron.orchestrator.router import (
    MAX_REPLANS,
    architecture_issues,
    replan_requested,
    route_after_review,
)


def test_route_approved():
    state = {"is_approved": True, "review_report": {}, "status": "running"}
    assert route_after_review(state) == "approved"


def test_route_exhausted():
    state = {"is_approved": False, "status": "exhausted", "review_report": {}}
    assert route_after_review(state) == "exhausted"


def test_route_to_architect_on_architecture_fix_target():
    state = {
        "is_approved": False,
        "status": "running",
        "review_report": {
            "issues": [
                {
                    "severity": "blocker",
                    "title": "Tasks split the module incorrectly",
                    "details": "card_deck.py and simulate_deal.py both define Deck",
                    "fix_target": "architecture",
                }
            ]
        },
    }
    assert route_after_review(state) == "architect"


def test_architecture_keywords_alone_do_not_replan():
    """Free-text wording no longer triggers a re-plan (it reset task progress)."""
    state = {
        "is_approved": False,
        "status": "running",
        "review_report": {
            "issues": [
                {
                    "severity": "blocker",
                    "title": "Architecture design flaw",
                    "details": "The overall approach and structure need restructuring",
                },
                {
                    "severity": "major",
                    "title": "Wrong approach in test",
                    "details": "Change the test design",
                    "fix_target": "test",
                },
            ]
        },
    }
    assert route_after_review(state) == "implementer"


def test_minor_architecture_fix_target_does_not_replan():
    state = {
        "is_approved": False,
        "status": "running",
        "review_report": {
            "issues": [
                {
                    "severity": "minor",
                    "title": "Consider splitting module",
                    "details": "",
                    "fix_target": "architecture",
                }
            ]
        },
    }
    assert route_after_review(state) == "implementer"


def test_route_to_implementer_on_code_issue():
    state = {
        "is_approved": False,
        "status": "running",
        "review_report": {
            "issues": [
                {
                    "severity": "major",
                    "title": "Missing null check",
                    "details": "Function crashes on None input",
                }
            ]
        },
    }
    assert route_after_review(state) == "implementer"


def test_route_to_implementer_when_no_issues():
    state = {
        "is_approved": False,
        "status": "running",
        "review_report": {"issues": []},
    }
    assert route_after_review(state) == "implementer"


def _architecture_state(**extra: Any) -> dict[str, Any]:
    issue = {"severity": "major", "title": "Split t3", "fix_target": "architecture"}
    return {
        "is_approved": False,
        "status": "running",
        "review_report": {"issues": [issue]},
        **extra,
    }


@pytest.mark.parametrize(
    "words",
    ["architecture", "design", "structure", "decomposition", "approach", "infrastructure"],
)
def test_wording_never_replans(words: str) -> None:
    issue = {
        "severity": "blocker",
        "title": f"Bad {words}",
        "details": f"structured output {words}",
    }
    state = {"is_approved": False, "status": "running", "review_report": {"issues": [issue]}}
    assert route_after_review(state) == "implementer"


def test_replans_stop_at_the_limit() -> None:
    assert route_after_review(_architecture_state(_replan_count=MAX_REPLANS - 1)) == "architect"
    assert route_after_review(_architecture_state(_replan_count=MAX_REPLANS)) == "implementer"
    assert route_after_review(_architecture_state(_replan_count=1), max_replans=1) == "implementer"


def test_architecture_issues_are_the_blocking_ones_targeting_the_plan() -> None:
    review = {
        "issues": [
            {"severity": "major", "title": "a", "fix_target": "architecture"},
            {"severity": "minor", "title": "b", "fix_target": "architecture"},
            {"severity": "blocker", "title": "c", "fix_target": "code"},
        ]
    }
    assert [i["title"] for i in architecture_issues(review)] == ["a"]
    assert architecture_issues(None) == []


def _requested_state(**extra: Any) -> dict[str, Any]:
    return {
        "is_approved": False,
        "status": "running",
        "review_report": {"issues": [{"severity": "minor", "title": "Naming"}]},
        "replan_request": {"source": "director", "reason": "t3 duplicates t2's module"},
        **extra,
    }


def test_replan_request_routes_to_architect() -> None:
    """The orchestrator's own request (#79), whatever the review says."""
    assert route_after_review(_requested_state()) == "architect"


def test_replan_request_respects_the_limit() -> None:
    assert route_after_review(_requested_state(_replan_count=MAX_REPLANS)) == "implementer"
    assert route_after_review(_requested_state(_replan_count=0), max_replans=0) == "implementer"
    assert route_after_review(_requested_state(_replan_count=2), max_replans=3) == "architect"


def test_approval_and_stops_outrank_a_replan_request() -> None:
    assert route_after_review(_requested_state(is_approved=True)) == "approved"
    assert route_after_review(_requested_state(status="stalled")) == "exhausted"


@pytest.mark.parametrize("request_value", [None, {}, "yes", {"source": "", "reason": ""}])
def test_no_or_malformed_replan_request(request_value: Any) -> None:
    state = _requested_state(replan_request=request_value)
    assert replan_requested(state) is False
    assert route_after_review(state) == "implementer"


def test_max_replans_defaults_to_the_module_limit() -> None:
    state = _architecture_state(_replan_count=MAX_REPLANS - 1)
    assert route_after_review(state, max_replans=None) == "architect"
