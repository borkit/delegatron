"""Tests for the pipeline observer (stuck detection and metrics)."""

from __future__ import annotations

from typing import Any

from delegatron.orchestrator.observer import (
    StuckDetectionConfig,
    StuckType,
    create_observer,
)


class TestConversationObserver:
    """Test the observer's stuck detection patterns."""

    def test_no_stuck_with_few_events(self) -> None:
        obs = create_observer()
        meta = {"_meta": {"input_tokens": 10, "output_tokens": 5}}
        obs.record_agent_call("architect", output=meta)
        assert obs.check_stuck() is None

    def test_error_loop_detected(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_error_repeat=3))
        for _ in range(3):
            obs.record_agent_call("implementer", error="LLM timeout")
        assert obs.check_stuck() == StuckType.ACTION_ERROR_LOOP

    def test_error_loop_not_triggered_below_threshold(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_error_repeat=3))
        obs.record_agent_call("implementer", error="LLM timeout")
        obs.record_agent_call("implementer", error="LLM timeout")
        assert obs.check_stuck() is None

    def test_review_overlap_stall(self) -> None:
        obs = create_observer()
        obs.record_review(
            {
                "acceptance_met": False,
                "issues": [
                    {"title": "Missing error handling"},
                    {"title": "No input validation"},
                ],
            }
        )
        # Same issues in next review = stall
        obs.record_review(
            {
                "acceptance_met": False,
                "issues": [
                    {"title": "Missing error handling"},
                    {"title": "No input validation"},
                ],
            }
        )
        assert obs.check_stuck() == StuckType.DELEGATRON_STALL

    def test_review_different_issues_no_stall(self) -> None:
        obs = create_observer()
        obs.record_review(
            {
                "acceptance_met": False,
                "issues": [{"title": "Missing error handling"}],
            }
        )
        obs.record_review(
            {
                "acceptance_met": False,
                "issues": [{"title": "Completely different issue"}],
            }
        )
        assert obs.check_stuck() is None

    def test_alternating_pattern_detected(self) -> None:
        obs = create_observer(StuckDetectionConfig(alternating_pattern=6))
        for _ in range(3):
            obs.record_agent_call("implementer", output={})
            obs.record_agent_call("reviewer", output={})
        assert obs.check_stuck() == StuckType.ALTERNATING_PATTERN

    def test_monologue_detected(self) -> None:
        obs = create_observer(StuckDetectionConfig(monologue_count=3))
        for _ in range(3):
            obs.record_agent_call("implementer", output={})
        assert obs.check_stuck() == StuckType.MONOLOGUE

    def test_healthy_pipeline_no_stuck(self) -> None:
        obs = create_observer()
        obs.record_agent_call("architect", output={"_meta": {}})
        obs.record_agent_call("implementer", output={"_meta": {}})
        obs.record_agent_call("reviewer", output={"_meta": {}})
        assert obs.check_stuck() is None


class TestObserverMetrics:
    """Test metrics collection."""

    def test_tracks_agent_calls(self) -> None:
        obs = create_observer()
        obs.record_agent_call(
            "architect",
            output={"_meta": {"input_tokens": 100, "output_tokens": 50}},
        )
        obs.record_agent_call(
            "implementer",
            output={"_meta": {"input_tokens": 200, "output_tokens": 100}},
        )

        metrics = obs.get_metrics()
        assert metrics["agent_calls"] == 2
        assert metrics["total_input_tokens"] == 300
        assert metrics["total_output_tokens"] == 150

    def test_tracks_errors(self) -> None:
        obs = create_observer()
        obs.record_agent_call("architect", error="API timeout")
        assert obs.get_metrics()["errors"] == 1

    def test_tracks_stalls(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_error_repeat=2))
        obs.record_agent_call("implementer", error="fail")
        obs.record_agent_call("implementer", error="fail")
        obs.check_stuck()
        assert obs.get_metrics()["stalls_detected"] == 1


class TestCondensationAdvisory:
    """Test context condensation hints."""

    def test_should_condense_when_near_limit(self) -> None:
        obs = create_observer()
        assert obs.should_condense(85_000, max_tokens=100_000) is True

    def test_should_not_condense_when_low(self) -> None:
        obs = create_observer()
        assert obs.should_condense(50_000, max_tokens=100_000) is False

    def test_threshold_is_80_percent(self) -> None:
        obs = create_observer()
        assert obs.should_condense(79_999, max_tokens=100_000) is False
        assert obs.should_condense(80_001, max_tokens=100_000) is True


class TestCreateObserver:
    """Test factory function."""

    def test_default_config(self) -> None:
        obs = create_observer()
        assert obs.config.action_error_repeat == 3
        assert obs.config.review_overlap_threshold == 0.7

    def test_custom_config(self) -> None:
        config = StuckDetectionConfig(action_error_repeat=5, monologue_count=10)
        obs = create_observer(config)
        assert obs.config.action_error_repeat == 5
        assert obs.config.monologue_count == 10


def _patch(content: str) -> dict[str, Any]:
    return {"files": {"simulate_deal.py": content}, "rationale": "fix"}


def _review(*titles: str) -> dict[str, Any]:
    return {"acceptance_met": False, "issues": [{"title": t} for t in titles]}


class TestOutputRepetition:
    """Stuck patterns judged on what the agents produced, not just call order."""

    def test_normal_implement_review_cycle_is_healthy(self) -> None:
        """Changing patches and changing reviews never look stuck."""
        obs = create_observer()
        obs.record_agent_call("architect", output={"tasks": []})
        for i in range(5):
            obs.record_agent_call("implementer", output=_patch(f"v{i}"))
            obs.record_review(_review(f"issue {i}"))
            assert obs.check_stuck() is None
        assert obs.get_metrics()["stalls_detected"] == 0

    def test_flip_flop_outputs_are_alternating(self) -> None:
        obs = create_observer()
        for content, title in [("v1", "too many prints"), ("v2", "too few prints")] * 2:
            obs.record_agent_call("implementer", output=_patch(content))
            obs.record_review(_review(title))
        obs.record_agent_call("implementer", output=_patch("v1"))
        obs.record_review(_review("too many prints"))
        assert obs.check_stuck() == StuckType.ALTERNATING_PATTERN

    def test_flip_flop_needs_reviews_to_repeat_too(self) -> None:
        obs = create_observer()
        for i, content in enumerate(["v1", "v2", "v1"]):
            obs.record_agent_call("implementer", output=_patch(content))
            obs.record_review(_review(f"issue {i}"))
        assert obs.check_stuck() is None

    def test_identical_implementer_output_is_action_observation_loop(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_observation_repeat=4))
        for i in range(4):
            output = {**_patch("same"), "_meta": {"input_tokens": i}}
            obs.record_agent_call("implementer", output=output)
            obs.record_review(_review(f"issue {i}"))
        assert obs.check_stuck() == StuckType.ACTION_OBSERVATION_LOOP

    def test_identical_output_below_threshold_is_not_a_loop(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_observation_repeat=4))
        for i in range(3):
            obs.record_agent_call("implementer", output=_patch("same"))
            obs.record_review(_review(f"issue {i}"))
        assert obs.check_stuck() is None

    def test_errors_are_not_repeated_output(self) -> None:
        obs = create_observer(StuckDetectionConfig(action_observation_repeat=2))
        obs.record_agent_call("implementer", error="timeout")
        obs.record_review(_review("a"))
        obs.record_agent_call("implementer", error="timeout")
        obs.record_review(_review("b"))
        assert obs.check_stuck() is None

    def test_reset_forgets_previous_task(self) -> None:
        obs = create_observer()
        obs.record_review(_review("Missing error handling"))
        obs.reset()
        obs.record_review(_review("Missing error handling"))
        assert obs.check_stuck() is None
        assert obs.get_metrics()["agent_calls"] == 2


class TestExplicitTokenCounts:
    """Token counts passed by the caller (graph.py pops ``_meta``)."""

    def test_explicit_counts(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_patch("x"), input_tokens=120, output_tokens=30)
        obs.record_review(_review("a"), input_tokens=80, output_tokens=20)
        metrics = obs.get_metrics()
        assert metrics["agent_calls"] == 2
        assert metrics["total_input_tokens"] == 200
        assert metrics["total_output_tokens"] == 50

    def test_explicit_counts_take_precedence_over_meta(self) -> None:
        obs = create_observer()
        output = {"_meta": {"input_tokens": 999, "output_tokens": 999}}
        obs.record_agent_call("implementer", output=output, input_tokens=10, output_tokens=5)
        metrics = obs.get_metrics()
        assert metrics["total_input_tokens"] == 10
        assert metrics["total_output_tokens"] == 5

    def test_review_counts_as_agent_call(self) -> None:
        obs = create_observer()
        obs.record_review(_review("a"))
        assert obs.get_metrics()["agent_calls"] == 1

    def test_a_step_counts_its_provider_calls_but_is_one_event(self) -> None:
        """Two-pass and parallel steps make several calls; patterns stay per step (#82)."""
        obs = create_observer()
        obs.record_agent_call("implementer", output=_patch("x"), calls=3, input_tokens=30)
        assert obs.get_metrics()["agent_calls"] == 3
        assert len(obs._recent_events) == 1

    def test_synthesized_review_adds_no_call(self) -> None:
        obs = create_observer()
        obs.record_review(_review("a"), calls=0)
        assert obs.get_metrics()["agent_calls"] == 0
        assert len(obs._recent_events) == 1


class TestNoopStreak:
    """ACTION_OBSERVATION_LOOP from the pipeline's no-op streak (#72)."""

    def test_two_noop_patches_in_a_row_are_a_loop(self) -> None:
        obs = create_observer()
        assert obs.config.action_observation_repeat == 2
        assert obs.check_stuck(noop_streak=2) == StuckType.ACTION_OBSERVATION_LOOP
        assert obs.get_metrics()["stalls_detected"] == 1

    def test_a_single_noop_patch_is_not_a_loop(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_patch("v1"))
        obs.record_review(_review("a"))
        assert obs.check_stuck(noop_streak=1) is None
        assert obs.check_stuck() is None

    def test_identical_outputs_keep_their_own_threshold(self) -> None:
        """Two identical patches are not yet a loop by output alone (the streak decides)."""
        obs = create_observer()
        for i in range(2):
            obs.record_agent_call("implementer", output=_patch("same"))
            obs.record_review(_review(f"issue {i}"))
        assert obs.check_stuck() is None


class TestReviewOverlap:
    """Review-overlap detection compares real reviewer verdicts by normalized title."""

    def test_titles_differing_in_case_or_punctuation_overlap(self) -> None:
        obs = create_observer()
        obs.record_review(_review("Missing error handling"))
        obs.record_review(_review("missing error-handling."))
        assert obs.check_stuck() == StuckType.DELEGATRON_STALL

    def test_synthesized_review_is_not_a_new_verdict(self) -> None:
        """A carried-forward review repeats the last one; it is no stall signal."""
        obs = create_observer()
        obs.record_agent_call("implementer", output=_patch("v1"))
        obs.record_review(_review("Incorrect number of print calls"))
        obs.record_agent_call("implementer", output=_patch("v1"))
        synthesized = {**_review("Incorrect number of print calls"), "synthesized": True}
        obs.record_review(synthesized, calls=0)
        assert obs.check_stuck() is None
        assert len(obs._recent_events) == 4
        assert obs.get_metrics()["agent_calls"] == 3


_META: dict[str, Any] = {"_meta": {"input_tokens": 999, "output_tokens": 999}}


class TestTokenFallback:
    """``_meta`` fills in tokens only when the caller passed none (#82)."""

    def test_explicit_zero_is_not_overridden(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_META, input_tokens=10, output_tokens=0)
        metrics = obs.get_metrics()
        assert metrics["total_input_tokens"] == 10
        assert metrics["total_output_tokens"] == 0

    def test_one_explicit_count_counts_the_other_as_zero(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_META, output_tokens=5)
        metrics = obs.get_metrics()
        assert (metrics["total_input_tokens"], metrics["total_output_tokens"]) == (0, 5)

    def test_meta_used_when_no_count_is_given(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_META)
        assert obs.get_metrics()["total_output_tokens"] == 999

    def test_a_step_without_provider_call_books_no_tokens(self) -> None:
        obs = create_observer()
        obs.record_agent_call("implementer", output=_META, calls=0)
        metrics = obs.get_metrics()
        assert metrics["agent_calls"] == 0
        assert metrics["total_input_tokens"] == metrics["total_output_tokens"] == 0
