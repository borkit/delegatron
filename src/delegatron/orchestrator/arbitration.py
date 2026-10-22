"""Director arbitration — resolves conflicts between agents.

When the implementer and reviewer disagree (e.g., the reviewer flags an
issue that the implementer has already tried and failed to fix), the
director (the architect's model, ``agents/director.py``) is brought in as a
judge to make a binding decision.  Its policy lives in
``prompts/director/v1.md``; this module only detects conflicts, builds the
director's input and applies its rulings.

This prevents infinite ping-pong between implement and review cycles.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any

from delegatron.orchestrator.scoring import TestEvidence, classify_test_evidence
from delegatron.orchestrator.termination import failure_keys, failures_recur, normalize_title
from delegatron.utils.test_output import format_test_failures

__all__ = [
    "ESCALATION_THRESHOLD",
    "MAX_FAILURE_EXCERPT",
    "RECURRING_FAILURE_TITLE",
    "apply_arbitration",
    "build_arbitration_input",
    "conflict_key",
    "detect_conflict",
    "normalize_title",
]

logger = logging.getLogger(__name__)

# After this many identical fixes for the same issue, escalate to director
ESCALATION_THRESHOLD = 2

# Title of the conflict raised for a recurring test failure the reviewer did not flag
RECURRING_FAILURE_TITLE = "Recurring test failure"

# Characters of failing-test output shown to the director
MAX_FAILURE_EXCERPT = 3000


def detect_conflict(
    current_review: dict[str, Any],
    previous_review: dict[str, Any] | None,
    iteration: int,
    *,
    current_signature: str = "",
    previous_signature: str = "",
    exclude: Collection[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Detect issues that persist across attempts (unresolved conflicts).

    *iteration* is the attempt count on the current task.  When the current
    attempt's test failure repeats the previous one (``failures_recur``: the
    same tests failing with the same assertion shape, so ``12 != 10`` then
    ``14 != 12`` recurs), the conflict is the failure itself: the current
    review's blocker/major issues, or one synthetic "Recurring test failure"
    issue when the reviewer raised none.  Otherwise returns the issues whose
    titles (ignoring case and punctuation) appeared in both the current and
    previous review, indicating the implementer failed to fix them.  Issues
    in *exclude* (the orchestrator's own gate issues: evidence, never
    arbitrated) are never returned.
    """
    if iteration < ESCALATION_THRESHOLD:
        return []

    issues = [issue for issue in current_review.get("issues", []) if issue not in exclude]
    if failures_recur(current_signature, previous_signature):
        conflicts = [
            issue for issue in issues if issue.get("severity") in ("blocker", "major")
        ] or [
            {
                "title": RECURRING_FAILURE_TITLE,
                "severity": "major",
                "details": current_signature,
                "recommended_fix": "",
            }
        ]
        logger.info("Detected recurring test failure across attempts: %s", current_signature)
        return conflicts

    if not previous_review:
        return []

    prev_titles = {normalize_title(i.get("title")) for i in previous_review.get("issues", [])}

    conflicts = []
    for issue in issues:
        title = normalize_title(issue.get("title"))
        if title and title in prev_titles:
            conflicts.append(issue)

    if conflicts:
        logger.info(
            "Detected %d recurring issues across iterations",
            len(conflicts),
        )

    return conflicts


def conflict_key(
    conflicts: list[dict[str, Any]], *, current_signature: str, previous_signature: str
) -> str:
    """Identity of a conflict found by ``detect_conflict``, so it is arbitrated only once.

    For a recurring test failure, its recurrence keys (``failure_keys``: the
    failing tests and their assertion shapes, so the same failure with moved
    values keeps its identity); otherwise the normalized titles of the
    recurring issues.
    """
    if failures_recur(current_signature, previous_signature):
        return "failure:" + "|".join(sorted(failure_keys(current_signature)))
    titles = sorted({normalize_title(issue.get("title")) for issue in conflicts})
    return "titles:" + "|".join(titles)


def build_arbitration_input(
    request: str,
    plan: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
    patch_rationale: str,
    test_passed: bool,
    *,
    task: dict[str, Any] | None = None,
    failing_tests: str = "",
    changed_files: Mapping[str, str] | None = None,
    test_evidence: TestEvidence | None = None,
) -> dict[str, Any]:
    """Build the director's input (see ``DirectorAgent`` for how it is rendered).

    The director sees the conflicting issues, the current task, the plan's
    acceptance criteria (``acceptance_criteria``; tasks carry none of their
    own), the implementer's rationale, the test result with the
    failing-test output (at most ``MAX_FAILURE_EXCERPT`` characters) and
    the files involved.  *test_evidence* (``classify_test_evidence``) tells
    a run that tested nothing (``untested``) apart from a failing one.
    """
    data: dict[str, Any] = {
        "request": request,
        "plan_summary": (plan or {}).get("summary", ""),
        "conflicts": conflicts,
        "implementer_rationale": patch_rationale,
        "test_passed": test_passed,
    }
    criteria = _acceptance_criteria(plan)
    if criteria:
        data["acceptance_criteria"] = criteria
    if test_evidence:
        data["test_evidence"] = test_evidence
    if task:
        data["task"] = task
    if failing_tests:
        data["failing_tests"] = format_test_failures(failing_tests, max_chars=MAX_FAILURE_EXCERPT)
    if changed_files:
        data["changed_files"] = dict(changed_files)
    return data


def _acceptance_criteria(plan: Mapping[str, Any] | None) -> list[str]:
    """The plan's acceptance criteria (``plan["acceptance"]["criteria"]``) as strings."""
    acceptance = (plan or {}).get("acceptance")
    criteria = acceptance.get("criteria") if isinstance(acceptance, Mapping) else None
    if isinstance(criteria, str):
        criteria = [criteria]
    if not isinstance(criteria, list):
        return []
    return [str(item) for item in criteria if str(item or "").strip()]


def apply_arbitration(
    state: dict[str, Any],
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply director's resolutions to the pipeline state.

    Resolutions match review issues by title (ignoring case and punctuation).
    Updates the review report to reflect the director's decisions:
    - ACCEPT: remove the issue from the review
    - REVISE: update the issue with the director's alternative (and the side
      to fix: ``fix_target``/``fix_file``)
    - DEFER: mark as deferred (won't block approval)

    The patch is approved only when no blocker/major issue remains and real
    tests passed on an applied patch: a ruling never outranks failing tests.
    """
    review = state.get("review_report") or {}
    issues = review.get("issues", [])

    resolved_titles = {}
    for res in resolutions:
        resolved_titles[normalize_title(res.get("title"))] = res

    updated_issues = []
    any_resolved = False
    for issue in issues:
        resolution = resolved_titles.get(normalize_title(issue.get("title")))

        if not resolution:
            updated_issues.append(issue)
            continue

        any_resolved = True

        decision = resolution.get("decision", "").upper()

        if decision == "ACCEPT":
            logger.info(
                "Director accepted: %s",
                issue.get("title"),
            )
            # Drop the issue — director overruled the reviewer
            continue

        elif decision == "REVISE":
            # Update the issue with the director's guidance
            issue["recommended_fix"] = resolution.get(
                "alternative", issue.get("recommended_fix", "")
            )
            issue["details"] = (
                f"[DIRECTOR REVISED] {resolution.get('reasoning', '')} "
                f"Original: {issue.get('details', '')}"
            )
            for key in ("fix_target", "fix_file"):
                if resolution.get(key):
                    issue[key] = resolution[key]
            updated_issues.append(issue)
            logger.info(
                "Director revised: %s",
                issue.get("title"),
            )

        elif decision == "DEFER":
            issue["severity"] = "nit"  # Downgrade so it won't block
            issue["details"] = (
                f"[DEFERRED] {resolution.get('reasoning', '')} Original: {issue.get('details', '')}"
            )
            updated_issues.append(issue)
            logger.info(
                "Director deferred: %s",
                issue.get("title"),
            )

        else:
            updated_issues.append(issue)

    review["issues"] = updated_issues

    # Re-evaluate acceptance: if no blockers/majors remain AND hard gates pass, approve
    blocking = [i for i in updated_issues if i.get("severity") in ("blocker", "major")]
    test_passed = classify_test_evidence(state.get("test_report")) == "passed"
    patch_applied = state.get("patch_applied", False)
    if not blocking and any_resolved and test_passed and patch_applied:
        review["acceptance_met"] = True
        state["is_approved"] = True
        logger.info("Director resolved all blocking issues — marking approved")

    state["review_report"] = review
    return state
