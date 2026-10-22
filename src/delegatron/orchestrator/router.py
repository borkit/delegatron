"""Conditional routing logic for the review loop."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Default number of re-plans a run may make (``route_after_review``'s
# *max_replans*; ``PipelineConfig.max_replans`` when configured); later
# re-plan requests go to the implementer.  Local models answer in plain JSON
# mode, so ``fix_target`` is not enforced by a schema and a run must not
# re-plan without bound.
MAX_REPLANS = 2


def architecture_issues(review: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The blocker/major issues of *review* whose fix is the plan (``fix_target`` "architecture").

    Re-planning keys only on this structured signal: free-text wording such as
    "approach" or "structure" is too common in ordinary code reviews.
    """
    issues: list[dict[str, Any]] = (review or {}).get("issues") or []
    return [
        issue
        for issue in issues
        if issue.get("severity") in ("blocker", "major")
        and issue.get("fix_target") == "architecture"
    ]


def replan_requested(state: Mapping[str, Any]) -> bool:
    """Whether the orchestrator asked for a re-plan (``replan_request``: ``{source, reason}``).

    Set by the orchestrator itself, e.g. when the director rules that the plan
    must change; a falsy or malformed value is no request.
    """
    request = state.get("replan_request")
    return isinstance(request, Mapping) and bool(request.get("source") or request.get("reason"))


def route_after_review(state: dict[str, Any], *, max_replans: int | None = None) -> str:
    """Determine next step after reviewer produces a ReviewReport.

    *max_replans* defaults to ``MAX_REPLANS``.

    Returns:
        "approved" — code passes review, proceed to finalize
        "architect" — a re-plan was requested (``replan_request``) or a
            blocker/major issue has fix_target "architecture", and the run
            has re-planned fewer than *max_replans* times (``_replan_count``)
        "implementer" — implementation issues found, fix code
        "exhausted" — max iterations or budget exceeded
    """
    limit = MAX_REPLANS if max_replans is None else max_replans
    # Check termination conditions
    if state.get("is_approved"):
        logger.info("Review approved — finalizing")
        return "approved"

    if state.get("status") in ("exhausted", "stalled", "error"):
        logger.info("Pipeline %s — stopping", state.get("status"))
        return "exhausted"

    requested = replan_requested(state)
    if requested or architecture_issues(state.get("review_report")):
        if (state.get("_replan_count") or 0) < limit:
            logger.info(
                "%s — routing to architect",
                "Re-plan requested" if requested else "Architectural issues found",
            )
            return "architect"
        logger.info("Re-plan limit (%d) reached — routing to implementer", limit)

    if (state.get("review_report") or {}).get("issues"):
        logger.info("Implementation issues found — routing to implementer")
    else:
        logger.info("Not yet approved — routing to implementer for next iteration")
    return "implementer"
