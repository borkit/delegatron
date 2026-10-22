"""Tests for director arbitration."""

from delegatron.orchestrator.arbitration import (
    MAX_FAILURE_EXCERPT,
    RECURRING_FAILURE_TITLE,
    apply_arbitration,
    build_arbitration_input,
    conflict_key,
    detect_conflict,
    normalize_title,
)


def test_detect_conflict_finds_recurring_issues():
    current = {
        "issues": [
            {"title": "Missing error handling", "severity": "minor"},
            {"title": "New issue", "severity": "major"},
        ]
    }
    previous = {
        "issues": [
            {"title": "Missing error handling", "severity": "minor"},
        ]
    }
    conflicts = detect_conflict(current, previous, iteration=2)
    assert len(conflicts) == 1
    assert conflicts[0]["title"] == "Missing error handling"


def test_detect_conflict_empty_on_first_iteration():
    current = {"issues": [{"title": "Some issue"}]}
    previous = {"issues": [{"title": "Some issue"}]}
    assert detect_conflict(current, previous, iteration=1) == []


def test_detect_conflict_empty_when_no_previous():
    current = {"issues": [{"title": "Issue"}]}
    assert detect_conflict(current, None, iteration=3) == []


def test_apply_arbitration_accept():
    state = {
        "review_report": {
            "issues": [
                {"title": "Minor style issue", "severity": "minor"},
            ],
            "acceptance_met": False,
        },
        "test_report": {"passed": True, "total": 3},
        "patch_applied": True,
        "is_approved": False,
    }
    resolutions = [
        {"title": "Minor style issue", "decision": "ACCEPT"},
    ]
    result = apply_arbitration(state, resolutions)
    # Issue should be removed
    assert len(result["review_report"]["issues"]) == 0
    # Should be approved (no blocking issues remain)
    assert result["is_approved"] is True


def test_apply_arbitration_revise():
    state = {
        "review_report": {
            "issues": [
                {
                    "title": "Bad approach",
                    "severity": "major",
                    "details": "Original details",
                    "recommended_fix": "Original fix",
                },
            ],
            "acceptance_met": False,
        },
        "is_approved": False,
    }
    resolutions = [
        {
            "title": "Bad approach",
            "decision": "REVISE",
            "alternative": "Use a different pattern",
            "reasoning": "The current approach is fine but could be better",
        },
    ]
    result = apply_arbitration(state, resolutions)
    issues = result["review_report"]["issues"]
    assert len(issues) == 1
    assert "Use a different pattern" in issues[0]["recommended_fix"]
    assert "[DIRECTOR REVISED]" in issues[0]["details"]


def test_apply_arbitration_defer():
    state = {
        "review_report": {
            "issues": [
                {"title": "Known limitation", "severity": "major", "details": "orig"},
            ],
            "acceptance_met": False,
        },
        "test_report": {"passed": True, "total": 3},
        "patch_applied": True,
        "is_approved": False,
    }
    resolutions = [
        {
            "title": "Known limitation",
            "decision": "DEFER",
            "reasoning": "Not critical for MVP",
        },
    ]
    result = apply_arbitration(state, resolutions)
    issues = result["review_report"]["issues"]
    assert len(issues) == 1
    assert issues[0]["severity"] == "nit"  # Downgraded
    assert "[DEFERRED]" in issues[0]["details"]
    # No blockers/majors remain, so approved
    assert result["is_approved"] is True


# --- detect_conflict keyed on failure signatures ----------------------------

SIGNATURE = (
    "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - AssertionError: 14 != 12"
)


def test_detect_conflict_same_failure_returns_blocking_issues():
    """Reworded titles still conflict when the failure itself recurs."""
    current = {
        "issues": [
            {"title": "Incorrect number of print calls in deal simulation", "severity": "major"},
            {"title": "Docstring missing", "severity": "minor"},
        ]
    }
    previous = {
        "issues": [
            {"title": "Incorrect number of print calls during simulation", "severity": "major"},
        ]
    }
    conflicts = detect_conflict(
        current, previous, 3, current_signature=SIGNATURE, previous_signature=SIGNATURE
    )
    assert [c["title"] for c in conflicts] == ["Incorrect number of print calls in deal simulation"]


def test_detect_conflict_same_failure_without_blocking_issue_is_synthetic():
    current = {"issues": [{"title": "Docstring missing", "severity": "minor"}]}
    conflicts = detect_conflict(
        current, None, 2, current_signature=SIGNATURE, previous_signature=SIGNATURE
    )
    assert conflicts == [
        {
            "title": "Recurring test failure",
            "severity": "major",
            "details": SIGNATURE,
            "recommended_fix": "",
        }
    ]


def test_detect_conflict_same_failure_respects_threshold():
    current = {"issues": [{"title": "A", "severity": "major"}]}
    assert (
        detect_conflict(current, None, 1, current_signature=SIGNATURE, previous_signature=SIGNATURE)
        == []
    )


def test_detect_conflict_moved_assertion_values_recur():
    """Run 6f29441bbe8c it6 -> it7: both sides of the assertion moved (12 != 10 -> 14 != 12).

    The same assertion shape on the same test is the same failure, so the
    director is called although the reviewer reworded its title.
    """
    it7 = {
        "issues": [
            {"title": "Incorrect number of print calls in deal simulation", "severity": "major"}
        ]
    }
    it6 = {
        "issues": [
            {"title": "Incorrect number of print calls during simulation", "severity": "major"}
        ],
        "synthesized": True,
    }
    sig6 = SIGNATURE.replace("14 != 12", "12 != 10")

    conflicts = detect_conflict(it7, it6, 4, current_signature=SIGNATURE, previous_signature=sig6)

    assert conflicts == it7["issues"]


def test_detect_conflict_changed_failure_falls_back_to_titles():
    """Run 6f29441bbe8c it4 -> it5: a different error is progress; titles decide."""
    title = "Incorrect number of print calls in deal simulation"
    current = {"issues": [{"title": title, "severity": "major"}]}
    previous = {"issues": [{"title": "Incorrect Mock Import", "severity": "major"}]}
    it4 = SIGNATURE.replace(
        "AssertionError: 14 != 12", "AttributeError: module 'unittest' has no attribute 'mock'"
    )
    conflicts = detect_conflict(
        current, previous, 3, current_signature=SIGNATURE, previous_signature=it4
    )
    assert conflicts == []
    previous = {"issues": [{"title": title, "severity": "major"}]}
    conflicts = detect_conflict(
        current, previous, 3, current_signature=SIGNATURE, previous_signature=it4
    )
    assert [c["title"] for c in conflicts] == [title]


def test_detect_conflict_extra_failing_test_still_recurs():
    """it5 -> it6: the flaky shuffle test failed only at it5; the deal failure repeats."""
    shuffle = "tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError: [A, B] == [A, B]"
    current = {"issues": [{"title": "Prints wrong", "severity": "major"}]}
    conflicts = detect_conflict(
        current,
        {"issues": [{"title": "Shuffle broken", "severity": "major"}]},
        3,
        current_signature=SIGNATURE,
        previous_signature=f"{shuffle}|{SIGNATURE}",
    )
    assert conflicts == current["issues"]


def test_detect_conflict_never_returns_excluded_gate_issues():
    """Gate issues are evidence: a recurring failure falls back to the synthetic conflict."""
    gate = {
        "title": "Existing tests removed: tests/test_a.py",
        "severity": "major",
        "source": "orchestrator",
    }
    current = {"issues": [gate, {"title": "Docstring missing", "severity": "minor"}]}
    previous = {"issues": [dict(gate)]}

    conflicts = detect_conflict(
        current,
        previous,
        3,
        current_signature=SIGNATURE,
        previous_signature=SIGNATURE,
        exclude=[gate],
    )
    assert [c["title"] for c in conflicts] == [RECURRING_FAILURE_TITLE]
    assert detect_conflict(current, previous, 3, exclude=[gate]) == []


def test_detect_conflict_empty_signatures_use_titles():
    current = {"issues": [{"title": "A", "severity": "major"}]}
    previous = {"issues": [{"title": "B", "severity": "major"}]}
    assert detect_conflict(current, previous, 3, current_signature="", previous_signature="") == []


# --- titles, conflict identity, director input and rulings (#72, #74) --------


def test_normalize_title_ignores_case_and_punctuation():
    a = "Incorrect number of print calls in deal simulation"
    b = "incorrect number of print-calls in deal simulation."
    assert normalize_title(a) == normalize_title(b)
    paraphrase = "Incorrect number of print calls during simulation"
    assert normalize_title(a) != normalize_title(paraphrase)


def test_detect_conflict_matches_titles_differing_in_case_or_punctuation():
    current = {"issues": [{"title": "Deck shuffle: no change!", "severity": "major"}]}
    previous = {"issues": [{"title": "deck shuffle no change", "severity": "major"}]}
    assert detect_conflict(current, previous, iteration=2) == current["issues"]


def test_conflict_key_is_the_failure_shape_or_the_titles():
    reworded = [{"title": "Print count wrong"}]
    key = conflict_key(reworded, current_signature=SIGNATURE, previous_signature=SIGNATURE)
    assert key.startswith(
        "failure:tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
    )
    # The same failure with moved values keeps its identity: arbitrated once.
    moved = SIGNATURE.replace("14 != 12", "16 != 14")
    assert conflict_key(reworded, current_signature=moved, previous_signature=SIGNATURE) == key
    by_title = conflict_key(
        [{"title": "B"}, {"title": "a."}], current_signature=SIGNATURE, previous_signature=""
    )
    assert by_title == "titles:a|b"
    assert by_title == conflict_key(
        [{"title": "A"}, {"title": "b"}], current_signature="", previous_signature=""
    )


def test_build_arbitration_input_carries_evidence_not_instructions():
    task = {"task_id": "t3", "title": "Simulate deal", "description": "print each hand"}
    failing = "x" * (MAX_FAILURE_EXCERPT * 3)

    data = build_arbitration_input(
        "deal cards",
        {"summary": "plan"},
        [{"title": RECURRING_FAILURE_TITLE}],
        "bumped the expectation",
        False,
        task=task,
        failing_tests=failing,
        changed_files={"simulate_deal.py": "print(1)\n"},
    )

    assert "instruction" not in data
    assert data["task"] == task
    assert len(data["failing_tests"]) <= MAX_FAILURE_EXCERPT
    assert data["changed_files"] == {"simulate_deal.py": "print(1)\n"}
    assert data["test_passed"] is False
    assert data["implementer_rationale"] == "bumped the expectation"


def test_build_arbitration_input_carries_the_plan_acceptance_criteria():
    """Tasks have no criteria of their own: the director gets the plan's (#74)."""
    plan = {
        "summary": "deal cards",
        "acceptance": {
            "criteria": ["The deck can be shuffled", "Each player's hand is printed"],
            "tests": [],
        },
    }

    data = build_arbitration_input("r", plan, [], "", False, test_evidence="untested")

    assert data["acceptance_criteria"] == [
        "The deck can be shuffled",
        "Each player's hand is printed",
    ]
    assert data["test_evidence"] == "untested"


def test_build_arbitration_input_tolerates_malformed_acceptance():
    for plan in ({"acceptance": None}, {"acceptance": {"criteria": None}}, {"acceptance": "x"}):
        assert "acceptance_criteria" not in build_arbitration_input("r", plan, [], "", True)
    single = build_arbitration_input("r", {"acceptance": {"criteria": "works"}}, [], "", True)
    assert single["acceptance_criteria"] == ["works"]


def test_build_arbitration_input_omits_empty_evidence():
    data = build_arbitration_input("r", None, [], "", True)
    assert set(data) == {
        "request",
        "plan_summary",
        "conflicts",
        "implementer_rationale",
        "test_passed",
    }


def test_apply_arbitration_revise_names_the_side_to_fix():
    state = {
        "review_report": {
            "issues": [{"title": "Print count wrong", "severity": "major", "details": "d"}],
        },
    }
    resolutions = [
        {
            "title": "print count wrong.",
            "decision": "REVISE",
            "alternative": "Expect 12 prints in the test",
            "fix_target": "test",
            "fix_file": "tests/test_simulate_deal.py",
        }
    ]

    issue = apply_arbitration(state, resolutions)["review_report"]["issues"][0]

    assert issue["recommended_fix"] == "Expect 12 prints in the test"
    assert issue["fix_target"] == "test"
    assert issue["fix_file"] == "tests/test_simulate_deal.py"


def test_apply_arbitration_never_approves_without_real_passing_tests():
    for report in ({"passed": False, "total": 3}, {"passed": True, "total": 0}, None):
        state = {
            "review_report": {"issues": [{"title": "A", "severity": "major"}]},
            "test_report": report,
            "patch_applied": True,
            "is_approved": False,
        }
        result = apply_arbitration(state, [{"title": "A", "decision": "ACCEPT"}])
        assert result["review_report"]["issues"] == []
        assert result["is_approved"] is False


def test_apply_arbitration_ignores_a_ruling_with_an_unknown_title():
    """A resolution whose title matches no issue changes nothing and approves nothing."""
    issue = {"title": "Print count wrong", "severity": "major", "details": "d"}
    state = {
        "review_report": {"issues": [dict(issue)], "acceptance_met": False},
        "test_report": {"passed": True, "total": 3},
        "patch_applied": True,
        "is_approved": False,
    }
    for decision in ("ACCEPT", "REVISE", "DEFER"):
        ruling = {"title": "Something the reviewer never said", "decision": decision}
        result = apply_arbitration(state, [ruling])
        assert result["review_report"]["issues"] == [issue]
        assert result["review_report"]["acceptance_met"] is False
        assert result["is_approved"] is False
