"""Pipeline observer for enhanced stuck detection and metrics.

Monitors pipeline execution as a passive **observer** alongside Delegatron's
authoritative AgentState. Collects signals but does NOT own pipeline state —
AgentState remains the single source of truth.

This module provides:
1. Enhanced stuck detection (4 pathological patterns beyond Delegatron's stall check)
2. Agent call metrics: calls and tokens as booked by the run's ``CostTracker``
   (the caller passes each step's usage), so they mirror ``cost_breakdown``
3. Context condensation hints (advisory, does not mutate state)

Usage in graph.py:
    observer = create_observer()
    # After each agent step (one event per step, however many provider calls):
    observer.record_agent_call(role, output=output_data, error=error,
                               calls=n, input_tokens=n_in, output_tokens=n_out)
    # After each review step (a skipped reviewer call counts calls=0):
    observer.record_review(review_report, calls=n, input_tokens=n_in, output_tokens=n_out)
    # Before routing (the task's run of patches that changed nothing):
    stuck_type = observer.check_stuck(noop_streak=state.get("_noop_streak", 0))
    if stuck_type:
        logger.warning("Stuck pattern detected: %s", stuck_type)
    # When advancing to the next task:
    observer.reset()
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from delegatron.orchestrator.termination import NOOP_STREAK_LIMIT, normalize_title

logger = logging.getLogger(__name__)


class StuckType(Enum):
    """Types of pathological states detected by the observer."""

    ACTION_OBSERVATION_LOOP = "action_observation_loop"
    ACTION_ERROR_LOOP = "action_error_loop"
    MONOLOGUE = "monologue"
    ALTERNATING_PATTERN = "alternating_pattern"
    DELEGATRON_STALL = "delegatron_stall"  # Original 70% overlap detection


@dataclass
class StuckDetectionConfig:
    """Thresholds for stuck detection patterns."""

    # Repetition thresholds
    # Same action→observation N times: a no-op streak of N (patches that
    # changed nothing; the stall policy's limit) ...
    action_observation_repeat: int = NOOP_STREAK_LIMIT
    # ... or N identical implementer outputs in a row
    repeated_output_repeat: int = 4
    action_error_repeat: int = 3  # Same action→error N times
    monologue_count: int = 3  # Agent messages without user input
    alternating_pattern: int = 6  # A→B→A→B cycling

    # Delegatron extension
    review_overlap_threshold: float = 0.7  # 70% issue overlap = stall


@dataclass
class ObserverMetrics:
    """Metrics collected by the observer."""

    agent_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    errors: int = 0
    stalls_detected: int = 0


@dataclass
class ConversationObserver:
    """Observer that monitors pipeline execution for stuck patterns.

    Does NOT own state — AgentState in graph.py is authoritative.
    This observer collects signals and provides advisory recommendations.
    """

    config: StuckDetectionConfig = field(default_factory=StuckDetectionConfig)
    metrics: ObserverMetrics = field(default_factory=ObserverMetrics)

    # Circular buffer of recent events for pattern detection
    _recent_events: list[dict[str, Any]] = field(default_factory=list)
    _max_history: int = 20

    def record_agent_call(
        self,
        role: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        calls: int = 1,
    ) -> None:
        """Record an agent step (*calls* provider requests) for pattern detection.

        Explicit token counts win (a missing one counts 0).  Only when both
        are ``None`` and the step made a provider call do the counts come from
        the ``_meta`` block of *output* (callers that left it in place).
        """
        if input_tokens is None and output_tokens is None and calls > 0:
            meta = (output or {}).get("_meta") or {}
            input_tokens = _token_count(meta.get("input_tokens"))
            output_tokens = _token_count(meta.get("output_tokens"))
        self._count_calls(calls, input_tokens or 0, output_tokens or 0)
        if error:
            self.metrics.errors += 1

        content = None if output is None else {k: v for k, v in output.items() if k != "_meta"}
        self._append_event(
            {
                "role": role,
                "had_output": output is not None,
                "had_error": error is not None,
                "error": error,
                "content_hash": _content_hash(content),
            }
        )

    def record_review(
        self,
        review: dict[str, Any],
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
    ) -> None:
        """Record a review step (*calls* reviewer requests) for stall detection.

        A review synthesized without a reviewer call counts ``calls=0``; it
        repeats an earlier verdict (``synthesized``), so it is one step of the
        loop but no new reviewer opinion: its titles are not compared for
        review overlap.  The review's content fingerprint is its verdict plus
        its issue titles (compared ignoring case and punctuation), so
        rewording the summary does not make a repeated review look new.
        """
        self._count_calls(calls, input_tokens, output_tokens)
        issues = review.get("issues", [])
        accepted = review.get("acceptance_met", False)
        titles = {normalize_title(i.get("title")) for i in issues} - {""}
        event: dict[str, Any] = {
            "role": "reviewer",
            "had_output": True,
            "had_error": False,
            "accepted": accepted,
            "issue_count": len(issues),
            "content_hash": _content_hash([accepted, sorted(titles)]),
        }
        if not review.get("synthesized"):
            event["issue_titles"] = titles
        self._append_event(event)

    def reset(self) -> None:
        """Forget recent events, e.g. when the pipeline advances to the next task.

        Patterns are judged per task; run-level metrics are kept.
        """
        self._recent_events = []

    def check_stuck(self, noop_streak: int = 0) -> StuckType | None:
        """Check for pathological patterns in recent events.

        *noop_streak* is the current task's run of consecutive patches that
        changed nothing (``_noop_streak`` in the pipeline state; the observer
        keeps no counter of its own).  Returns the type of stuck pattern
        detected, or None if healthy.
        """
        # Pattern 0: Patches that changed nothing, N in a row
        if noop_streak >= self.config.action_observation_repeat:
            self.metrics.stalls_detected += 1
            return StuckType.ACTION_OBSERVATION_LOOP

        if len(self._recent_events) < 2:
            return None

        # Pattern 1: Same role producing errors repeatedly
        error_streak = self._count_trailing_errors()
        if error_streak >= self.config.action_error_repeat:
            self.metrics.stalls_detected += 1
            return StuckType.ACTION_ERROR_LOOP

        # Pattern 2: Implementer resubmitting identical output
        if self._detect_repeated_output():
            self.metrics.stalls_detected += 1
            return StuckType.ACTION_OBSERVATION_LOOP

        # Pattern 3: Reviewer rejecting the same issues (Delegatron stall)
        if self._detect_review_overlap():
            self.metrics.stalls_detected += 1
            return StuckType.DELEGATRON_STALL

        # Pattern 4: Two roles cycling through the same outputs (A→B→A→B)
        if self._detect_alternating_pattern():
            self.metrics.stalls_detected += 1
            return StuckType.ALTERNATING_PATTERN

        # Pattern 5: Same role called repeatedly without progress
        if self._detect_monologue():
            self.metrics.stalls_detected += 1
            return StuckType.MONOLOGUE

        return None

    def should_condense(self, total_tokens: int, max_tokens: int = 100_000) -> bool:
        """Advisory: suggest context condensation if approaching token limits.

        This does NOT condense — it only signals to the caller.
        The pipeline decides whether to act on this signal.
        """
        return total_tokens > max_tokens * 0.8

    def get_metrics(self) -> dict[str, Any]:
        """Return observer metrics as a dict for logging/audit."""
        return {
            "agent_calls": self.metrics.agent_calls,
            "total_input_tokens": self.metrics.total_input_tokens,
            "total_output_tokens": self.metrics.total_output_tokens,
            "errors": self.metrics.errors,
            "stalls_detected": self.metrics.stalls_detected,
        }

    def _count_calls(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        """Count a step's agent calls and their tokens."""
        self.metrics.agent_calls += calls
        self.metrics.total_input_tokens += input_tokens
        self.metrics.total_output_tokens += output_tokens

    def _append_event(self, event: dict[str, Any]) -> None:
        """Append *event* to the bounded history."""
        self._recent_events.append(event)
        if len(self._recent_events) > self._max_history:
            self._recent_events = self._recent_events[-self._max_history :]

    def _count_trailing_errors(self) -> int:
        """Count consecutive error events at the end of history."""
        count = 0
        for event in reversed(self._recent_events):
            if event.get("had_error"):
                count += 1
            else:
                break
        return count

    def _detect_review_overlap(self) -> bool:
        """Detect when reviewer raises the same issues across iterations.

        Mirrors Delegatron's detect_stall() but uses the observer's event history.
        """
        reviews = [
            e for e in self._recent_events if e.get("role") == "reviewer" and "issue_titles" in e
        ]
        if len(reviews) < 2:
            return False

        current = reviews[-1].get("issue_titles", set())
        previous = reviews[-2].get("issue_titles", set())

        if not current or not previous:
            return False

        overlap = len(current & previous) / max(len(current), 1)
        return overlap >= self.config.review_overlap_threshold

    def _detect_repeated_output(self) -> bool:
        """Detect the implementer returning identical output N times in a row."""
        repeat = self.config.repeated_output_repeat
        hashes = [e.get("content_hash") for e in self._recent_events if e["role"] == "implementer"]
        recent = hashes[-repeat:]
        return len(recent) == repeat and recent[0] is not None and len(set(recent)) == 1

    def _detect_alternating_pattern(self) -> bool:
        """Detect A→B→A→B cycling between two roles whose outputs repeat.

        Strict role alternation alone is the normal implementer→reviewer loop,
        so each role's outputs in the window must also cycle with period two
        (X, Y, X, ... — identical outputs count too).  That takes at least
        three calls per role, i.e. a window of six or more events.
        """
        if len(self._recent_events) < self.config.alternating_pattern:
            return False

        recent = self._recent_events[-self.config.alternating_pattern :]
        roles = [e["role"] for e in recent]

        # Check if roles alternate between exactly 2 values
        unique = set(roles)
        if len(unique) != 2:
            return False

        # Check strict alternation
        if any(roles[i] == roles[i - 1] for i in range(1, len(roles))):
            return False

        for role in unique:
            hashes = [e.get("content_hash") for e in recent if e["role"] == role]
            if len(hashes) < 3 or None in hashes:
                return False
            if any(hashes[k] != hashes[k - 2] for k in range(2, len(hashes))):
                return False
        return True

    def _detect_monologue(self) -> bool:
        """Detect same role called repeatedly without other roles intervening."""
        if len(self._recent_events) < self.config.monologue_count:
            return False

        recent = self._recent_events[-self.config.monologue_count :]
        roles = {e["role"] for e in recent}
        return len(roles) == 1


def _token_count(value: Any) -> int:
    """A ``_meta`` token count as an int (0 for a missing or malformed value)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _content_hash(content: Any) -> str | None:
    """Return a stable fingerprint of *content*, or None when there is none."""
    if content is None:
        return None
    encoded = json.dumps(content, sort_keys=True, default=str).encode()
    return hashlib.sha1(encoded, usedforsecurity=False).hexdigest()


def create_observer(
    config: StuckDetectionConfig | None = None,
) -> ConversationObserver:
    """Create a ConversationObserver with optional custom thresholds."""
    return ConversationObserver(
        config=config or StuckDetectionConfig(),
    )
