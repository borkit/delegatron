"""Termination criteria and stall detection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Consecutive patches that changed nothing before the task counts as stalled.
NOOP_STREAK_LIMIT = 2

# Share of the current attempt's failures (or issue titles) that must have
# occurred in the previous one for the attempt to count as a repeat.
RECURRENCE_OVERLAP = 0.7

_NON_WORD = re.compile(r"[\W_]+")

# Values that change between runs of the same failure: memory addresses,
# durations, quoted literals and numbers ("12 != 10" and "14 != 12" are one
# assertion shape).  Applied in this order.
_VOLATILE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (
        re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ms|us|ns|s|secs?|seconds?|minutes?|min)(?![\w])"),
        "<duration>",
    ),
    (re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\""""), "<str>"),
    (re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])"), "<n>"),
)
_MAX_KEY = 200


def should_terminate(state: dict[str, Any], max_iterations: int, max_cost_usd: float) -> bool:
    """Check if the pipeline should stop.

    Termination conditions:
    1. Reviewer approved the code
    2. Max iterations reached
    3. Cost budget exceeded

    Stalls are judged per task by ``should_stop_for_stall``.
    """
    if state.get("is_approved"):
        return True

    if state.get("iteration", 0) >= max_iterations:
        return True

    # max_cost_usd <= 0 means unlimited (e.g. local-only Ollama runs)
    if max_cost_usd <= 0:
        return False
    return bool(state.get("total_cost_usd", 0.0) >= max_cost_usd)


def normalize_title(title: object) -> str:
    """Issue title compared case- and punctuation-insensitively."""
    return _NON_WORD.sub(" ", str(title or "").casefold()).strip()


def mask_volatile(text: str) -> str:
    """*text* with addresses, durations, quoted literals and numbers masked.

    Whitespace is collapsed and the result capped, so two runs of the same
    failure yield the same text even when the values in it moved.
    """
    masked = " ".join(text.split())
    for pattern, placeholder in _VOLATILE:
        masked = pattern.sub(placeholder, masked)
    return masked[:_MAX_KEY]


def failure_keys(signature: str) -> frozenset[str]:
    """Per-test recurrence keys of a failure signature (``utils.test_output.failure_signature``).

    The signature joins one part per failing test with ``"|"``: a node id,
    optionally followed by ``" - "`` and its first error line.  Each key is
    the test id plus the *shape* of that error line (``mask_volatile``), so
    a failure whose asserted values moved is the same failure.  A part that
    is not a test id (the last output line of an unattributable failure) is
    masked whole.  Empty for a passing attempt (``""``).
    """
    keys: set[str] = set()
    for part in signature.split("|"):
        part = part.strip()
        if not part:
            continue
        test, sep, error = part.partition(" - ")
        if sep and test and not any(c.isspace() for c in test):
            keys.add(f"{test} - {mask_volatile(error)}")
        elif any(c.isspace() for c in part):
            keys.add(mask_volatile(part))
        else:
            keys.add(part)
    return frozenset(keys)


def failures_recur(current: str, previous: str) -> bool:
    """Whether the *current* failure repeats the *previous* one.

    Both are failure signatures (``""`` = no failure).  True when at least
    ``RECURRENCE_OVERLAP`` of the current failure's keys (``failure_keys``)
    also occurred previously: an extra or fixed test (a flaky one, say) does
    not hide a repeat, and the same assertion with moved values recurs.
    """
    now, before = failure_keys(current), failure_keys(previous)
    if not now or not before:
        return False
    return len(now & before) / len(now) >= RECURRENCE_OVERLAP


def detect_stall(
    current_review: dict[str, Any],
    previous_review: dict[str, Any] | None,
    iteration: int = 0,
    min_iterations: int = 3,
) -> bool:
    """Detect if the pipeline is stuck repeating the same issues.

    Returns True if 70%+ of current issues appeared in the previous review
    (titles compared by ``normalize_title``, so a change of case or
    punctuation is the same issue).  Skips detection before *min_iterations*
    to give local models time to converge.
    """
    if previous_review is None:
        return False

    # Don't trigger stall detection too early
    if iteration < min_iterations:
        return False

    current_titles = {normalize_title(i.get("title")) for i in current_review.get("issues", [])}
    previous_titles = {normalize_title(i.get("title")) for i in previous_review.get("issues", [])}

    # Discard empty-string titles to avoid false stall detection
    # when issues lack a title field
    current_titles.discard("")
    previous_titles.discard("")

    if not current_titles:
        return False

    overlap = current_titles & previous_titles
    overlap_ratio = len(overlap) / len(current_titles)
    return overlap_ratio >= RECURRENCE_OVERLAP


def should_stop_for_stall(state: Mapping[str, Any], *, min_attempts: int) -> str | None:
    """Return why the current task is stalled, or None to keep iterating.

    The single stall policy, evaluated after each reviewed attempt.  Reads
    ``_task_iteration`` (attempts on the current task, already incremented),
    ``_noop_streak``, ``_failure_signatures`` (current task, newest last),
    ``review_report``, ``previous_review``, ``_arbitration_pending`` and
    ``is_approved`` from *state*.

    Returns:
        None — approved, a director ruling still awaits its attempt, fewer
            than *min_attempts* attempts so far, a synthesized review (it
            repeats an earlier verdict: the attempt changed nothing or its
            agent call failed), or no recurrence found
        "no_progress" — the last ``NOOP_STREAK_LIMIT`` patches changed
            nothing (checked regardless of attempts)
        "recurring_failure" — the latest attempt failed its tests like the
            previous failing attempt (``failures_recur``: mostly the same
            tests, with the same assertion shape)
        "recurring_issues" — the tests gave no failure signal and the
            reviewer repeated its previous issues (see ``detect_stall``)
    """
    if state.get("is_approved") or state.get("_arbitration_pending"):
        return None

    if (state.get("_noop_streak") or 0) >= NOOP_STREAK_LIMIT:
        return "no_progress"

    attempts = state.get("_task_iteration") or 0
    if attempts < min_attempts or (state.get("review_report") or {}).get("synthesized"):
        return None

    history: list[str] = state.get("_failure_signatures") or []
    if history and history[-1]:
        # Failing tests outrank reviewer wording: recurrence is keyed on the
        # failure itself, so a reworded review of the same failure still
        # recurs and a repeated title over a different failure does not.
        failing = [signature for signature in history if signature]
        if len(failing) >= 2 and failures_recur(failing[-1], failing[-2]):
            return "recurring_failure"
        return None

    review = state.get("review_report")
    if review and detect_stall(
        review,
        state.get("previous_review"),
        iteration=attempts,
        min_iterations=min_attempts,
    ):
        return "recurring_issues"
    return None
