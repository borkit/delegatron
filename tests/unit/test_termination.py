"""Tests for termination logic."""

from typing import Any

import pytest

from delegatron.orchestrator.termination import (
    detect_stall,
    failure_keys,
    failures_recur,
    mask_volatile,
    normalize_title,
    should_stop_for_stall,
    should_terminate,
)
from delegatron.utils.test_output import failure_signature


def test_terminate_when_approved():
    state = {"is_approved": True, "iteration": 1, "total_cost_usd": 0.5}
    assert should_terminate(state, max_iterations=5, max_cost_usd=5.0) is True


def test_terminate_when_max_iterations():
    state = {"is_approved": False, "iteration": 5, "total_cost_usd": 0.5}
    assert should_terminate(state, max_iterations=5, max_cost_usd=5.0) is True


def test_terminate_when_budget_exceeded():
    state = {"is_approved": False, "iteration": 1, "total_cost_usd": 6.0}
    assert should_terminate(state, max_iterations=5, max_cost_usd=5.0) is True


def test_no_terminate_when_in_progress():
    state = {"is_approved": False, "iteration": 2, "total_cost_usd": 1.0}
    assert should_terminate(state, max_iterations=5, max_cost_usd=5.0) is False


def test_no_terminate_when_zero_budget():
    """max_cost_usd=0 means unlimited (e.g. local Ollama runs)."""
    state = {"is_approved": False, "iteration": 1, "total_cost_usd": 0.001}
    assert should_terminate(state, max_iterations=5, max_cost_usd=0) is False
    assert should_terminate(state, max_iterations=5, max_cost_usd=0.0) is False


def test_no_stall_below_threshold():
    current = {"issues": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}
    previous = {"issues": [{"title": "A"}, {"title": "B"}, {"title": "D"}]}
    assert detect_stall(current, previous, iteration=5) is False  # 2/3 = 66.7% < 70%


def test_stall_high_overlap():
    current = {"issues": [{"title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "D"}]}
    previous = {"issues": [{"title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "E"}]}
    # 3/4 = 75% >= 70%, but only triggers after min_iterations (default 3)
    assert detect_stall(current, previous, iteration=1) is False
    assert detect_stall(current, previous, iteration=3) is True


def test_no_stall_first_iteration():
    current = {"issues": [{"title": "A"}]}
    assert detect_stall(current, None) is False


def test_stall_titles_compared_ignoring_case_and_punctuation():
    """The no-test-signal fallback: a re-cased or re-punctuated title is the same issue (#72)."""
    current = {"issues": [{"title": "incorrect number of print calls in deal simulation."}]}
    previous = {"issues": [{"title": "Incorrect number of print calls in deal simulation"}]}
    assert normalize_title(current["issues"][0]["title"]) == normalize_title(
        previous["issues"][0]["title"]
    )
    assert detect_stall(current, previous, iteration=3) is True
    paraphrase = {"issues": [{"title": "Incorrect number of print calls during simulation"}]}
    assert detect_stall(paraphrase, previous, iteration=3) is False


# --- should_stop_for_stall -------------------------------------------------


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "is_approved": False,
        "_arbitration_pending": False,
        "_noop_streak": 0,
        "_task_iteration": 5,
        "_failure_signatures": [],
        "review_report": {"issues": [{"title": "A"}]},
        "previous_review": {"issues": [{"title": "B"}]},
    }
    state.update(overrides)
    return state


SAME_TITLES: dict[str, Any] = {
    "review_report": {"issues": [{"title": "A"}]},
    "previous_review": {"issues": [{"title": "A"}]},
}


def test_stall_policy_none_when_approved():
    state = _state(is_approved=True, _noop_streak=3, _failure_signatures=["x", "x"])
    assert should_stop_for_stall(state, min_attempts=3) is None


def test_stall_policy_none_while_arbitration_pending():
    state = _state(_arbitration_pending=True, _noop_streak=3, _failure_signatures=["x", "x"])
    assert should_stop_for_stall(state, min_attempts=3) is None


def test_stall_policy_noop_streak_stops_regardless_of_attempts():
    state = _state(_task_iteration=1, _noop_streak=2)
    assert should_stop_for_stall(state, min_attempts=5) == "no_progress"


def test_stall_policy_single_noop_is_not_a_stall():
    assert should_stop_for_stall(_state(_noop_streak=1), min_attempts=3) is None


def test_stall_policy_grace_before_min_attempts():
    state = _state(_task_iteration=4, _failure_signatures=["x", "x"], **SAME_TITLES)
    assert should_stop_for_stall(state, min_attempts=5) is None


def test_stall_policy_recurring_failure():
    state = _state(_failure_signatures=["x", "y", "y"])
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_failure"


def test_stall_policy_recurring_failure_skips_passing_attempts():
    state = _state(_failure_signatures=["y", "", "y"])
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_failure"


def test_stall_policy_changed_failure_ignores_repeated_titles():
    """With a test-failure signal, reviewer titles are not consulted."""
    state = _state(_failure_signatures=["x", "y"], **SAME_TITLES)
    assert should_stop_for_stall(state, min_attempts=3) is None


def test_stall_policy_passing_attempt_ends_failure_recurrence():
    """Tests pass now: earlier identical failures no longer count."""
    state = _state(_failure_signatures=["x", "x", ""])
    assert should_stop_for_stall(state, min_attempts=3) is None


def test_stall_policy_recurring_issues_without_test_signal():
    state = _state(_failure_signatures=["x", ""], **SAME_TITLES)
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_issues"
    state = _state(_failure_signatures=[], **SAME_TITLES)
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_issues"


def test_stall_policy_no_review_no_stall():
    state = _state(review_report=None, previous_review=None)
    assert should_stop_for_stall(state, min_attempts=3) is None


def test_stall_policy_synthesized_review_only_checks_noop_streak():
    """A synthesized review repeats an earlier verdict; the attempt may be untested."""
    synthesized = {"issues": [{"title": "A"}], "synthesized": True}
    for signatures in (["x", "x"], ["x", ""]):
        state = _state(
            _failure_signatures=signatures,
            review_report=synthesized,
            previous_review={"issues": [{"title": "A"}]},
        )
        assert should_stop_for_stall(state, min_attempts=3) is None
    state = _state(_noop_streak=2, review_report=synthesized)
    assert should_stop_for_stall(state, min_attempts=3) == "no_progress"


def test_stall_policy_missing_keys_read_as_defaults():
    assert should_stop_for_stall({}, min_attempts=0) is None


def test_stall_policy_title_fallback_is_normalized():
    state = _state(
        _failure_signatures=[],
        review_report={"issues": [{"title": "Deck shuffle: no change!"}]},
        previous_review={"issues": [{"title": "deck shuffle - no change"}]},
    )
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_issues"


# --- failure recurrence keys (#72, #74, D11) --------------------------------

DEAL = "tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal"
SHUFFLE = "tests/test_card_deck.py::TestDeck::test_shuffle"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("AssertionError: 12 != 10", "AssertionError: 14 != 12"),
        ("assert 0.5 == 1.25", "assert -3 == 2e10"),
        ("<Card object at 0x7f3a12bc90> is not None", "<Card object at 0x10ab3c4d50> is not None"),
        ("Timeout after 1.5s", "Timeout after 30 ms"),
        ("KeyError: 'alice'", 'KeyError: "bob"'),
    ],
)
def test_mask_volatile_keeps_only_the_shape(first: str, second: str) -> None:
    assert mask_volatile(first) == mask_volatile(second)


def test_mask_volatile_keeps_identifiers_and_exception_types():
    assert mask_volatile("x1 != y2") == "x1 != y2"
    assert mask_volatile("AssertionError: 12 != 10") != mask_volatile(
        "AttributeError: module 'unittest' has no attribute 'mock'"
    )


def test_failure_keys_mask_the_error_but_not_the_test_id():
    keys = failure_keys(
        f"{DEAL}[1] - AssertionError: 12 != 10|{DEAL}[2] - AssertionError: 14 != 12"
    )
    assert keys == {
        f"{DEAL}[1] - AssertionError: <n> != <n>",
        f"{DEAL}[2] - AssertionError: <n> != <n>",
    }
    assert failure_keys("") == frozenset()
    assert failure_keys("TestParse|TestFormat") == {"TestParse", "TestFormat"}
    assert failure_keys("Error: exit status 2") == {"Error: exit status <n>"}


def test_failures_recur_on_the_same_assertion_shape():
    it6 = f"{DEAL} - AssertionError: 12 != 10"
    it7 = f"{DEAL} - AssertionError: 14 != 12"
    it4 = f"{DEAL} - AttributeError: module 'unittest' has no attribute 'mock'"
    assert failures_recur(it7, it6)
    assert not failures_recur(it6, it4)
    assert not failures_recur(it6, "")
    assert not failures_recur("", it6)


def test_failures_recur_when_most_current_failures_repeat():
    """An extra failure in the previous attempt (a flaky test) does not hide a repeat."""
    deal = f"{DEAL} - AssertionError: 12 != 10"
    shuffle = f"{SHUFFLE} - AssertionError: [A, B] == [A, B]"
    assert failures_recur(deal, f"{shuffle}|{deal}")
    # A new failure next to the old one: half of the current failures are new.
    assert not failures_recur(f"{shuffle}|{deal}", deal)
    three = "|".join(f"tests/t.py::test_{i} - E" for i in range(3))
    four = f"{three}|tests/t.py::test_new - E"
    assert failures_recur(four, three)  # 3 of 4 = 75% >= 70%


def test_stall_policy_recurring_failure_with_moved_values():
    state = _state(
        _failure_signatures=[
            f"{DEAL} - AssertionError: 12 != 10",
            f"{DEAL} - AssertionError: 14 != 12",
        ]
    )
    assert should_stop_for_stall(state, min_attempts=3) == "recurring_failure"


def test_stall_policy_different_error_is_progress():
    state = _state(
        _failure_signatures=[
            f"{DEAL} - AttributeError: module 'unittest' has no attribute 'mock'",
            f"{DEAL} - AssertionError: 12 != 10",
        ],
        **SAME_TITLES,
    )
    assert should_stop_for_stall(state, min_attempts=3) is None


# Replay of run 6f29441bbe8c, task t3 (its first attempt was it4). The pytest
# output excerpts are the run's real FAILURES sections, tracebacks trimmed.
_IT5_OUTPUT = """\
=================================== FAILURES ===================================
____________________________ TestDeck.test_shuffle _____________________________

>       self.assertNotEqual(original_cards, self.deck.cards)
E       AssertionError: [Card(name=Pikachu, type=Electric), Card(name=Eevee, type=Normal)] == \
[Card(name=Pikachu, type=Electric), Card(name=Eevee, type=Normal)]

tests/test_card_deck.py:15: AssertionError
_____________________ TestSimulateDeal.test_simulate_deal ______________________

>           self.assertEqual(len(calls), 10)  # 5 cards for Player 1, 5 cards for Player 2
E           AssertionError: 12 != 10

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_card_deck.py::TestDeck::test_shuffle - AssertionError: [Car...
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 2 failed, 5 passed in 0.23s ==========================
"""
_IT6_OUTPUT = """\
=================================== FAILURES ===================================
_____________________ TestSimulateDeal.test_simulate_deal ______________________

>           self.assertEqual(len(calls), 10)  # 5 cards for Player 1, 5 cards for Player 2
E           AssertionError: 12 != 10

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 1 failed, 6 passed in 0.21s ==========================
"""
_IT7_OUTPUT = """\
=================================== FAILURES ===================================
_____________________ TestSimulateDeal.test_simulate_deal ______________________

>           self.assertEqual(len(calls), 12)  # 5 cards for Player 1, 5 cards for Player 2
E           AssertionError: 14 != 12

tests/test_simulate_deal.py:33: AssertionError
=========================== short test summary info ============================
FAILED tests/test_simulate_deal.py::TestSimulateDeal::test_simulate_deal - As...
========================= 1 failed, 6 passed in 0.22s ==========================
"""
_REPLAY = [
    (
        _IT5_OUTPUT,
        [
            "Deck shuffle method does not alter card order",
            "Incorrect number of print calls during simulation",
        ],
    ),
    (_IT6_OUTPUT, ["Incorrect number of print calls in deal simulation"]),
    (_IT7_OUTPUT, ["Incorrect number of print calls in deal simulation"]),
]


def _replay_states() -> list[dict[str, Any]]:
    """State after each reviewed t3 attempt it5..it7 (attempts 2..4)."""
    states: list[dict[str, Any]] = []
    signatures: list[str] = []
    previous: dict[str, Any] | None = None
    for attempt, (stdout, titles) in enumerate(_REPLAY, start=2):
        signatures.append(failure_signature({"passed": False, "stdout": stdout, "stderr": ""}))
        review = {
            "acceptance_met": False,
            "issues": [{"severity": "major", "title": title} for title in titles],
        }
        states.append(
            {
                "is_approved": False,
                "_task_iteration": attempt,
                "_noop_streak": 0,
                "_failure_signatures": list(signatures),
                "review_report": review,
                "previous_review": previous,
            }
        )
        previous = review
    return states


def test_replay_run_failure_signature_keeps_the_assertion_shape():
    signatures = _replay_states()[-1]["_failure_signatures"]
    # it6 -> it7 moved both sides of the assertion (12 != 10 -> 14 != 12):
    # the same failure shape, so the same signature.
    assert signatures[1].endswith("AssertionError: <num> != <num>")
    assert signatures[2] == signatures[1]
    assert signatures[0] != signatures[1]  # it5 also failed the flaky shuffle test


def test_replay_run_no_longer_stalls_at_it7():
    states = _replay_states()
    it7 = states[-1]
    # The old check stopped the run here: global iteration 7 >= 5, same title.
    assert detect_stall(it7["review_report"], it7["previous_review"], 7, 5) is True
    # Per task, t3 is only on its 4th attempt (local grace is 5) ...
    assert all(should_stop_for_stall(s, min_attempts=5) is None for s in states)
    # ... and past a grace of 3 the failure itself repeats: it6 repeats it5
    # (only the flaky shuffle test went away) and it7 moved both sides of
    # the assertion (12 != 10 -> 14 != 12).
    assert [should_stop_for_stall(s, min_attempts=3) for s in states] == [
        None,  # it5: attempt 2, inside the grace
        "recurring_failure",
        "recurring_failure",
    ]


def test_replay_it5_to_it6_recurs_although_the_titles_differ():
    it6_state = _replay_states()[1]
    it5, it6 = it6_state["_failure_signatures"]
    assert failures_recur(it6, it5)
    assert not detect_stall(
        it6_state["review_report"], it6_state["previous_review"], iteration=3, min_iterations=3
    )


def test_replay_run_stops_when_failure_repeats():
    it8 = _replay_states()[-1]
    it8["_task_iteration"] = 5
    it8["_failure_signatures"].append(it8["_failure_signatures"][-1])
    it8["previous_review"] = it8["review_report"]
    assert should_stop_for_stall(it8, min_attempts=5) == "recurring_failure"
