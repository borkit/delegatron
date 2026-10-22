"""Agent state definition for the LangGraph state machine."""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict


class AgentState(TypedDict):
    """Shared state flowing through the LangGraph pipeline.

    Uses annotated reducers:
    - messages: list-append (preserves audit trail)
    - Everything else: replace (always latest version)

    Every key except request/project_info/file_tree/workspace_root/status/error
    and the per-session _termination_reason/_agent_failures is restored from a
    checkpoint on resume (``graph.RESUME_STATE_KEYS``).
    """

    # Audit trail of all events
    messages: Annotated[list[dict[str, Any]], operator.add]

    # Original user request
    request: str

    # Project context
    project_info: dict[str, Any]
    file_tree: list[str]
    workspace_root: str

    # Agent outputs (replaced each iteration)
    plan: dict[str, Any] | None
    current_task: dict[str, Any] | None
    current_patch: dict[str, Any] | None
    test_report: dict[str, Any] | None
    review_report: dict[str, Any] | None
    previous_review: dict[str, Any] | None

    # Loop control
    iteration: int
    is_approved: bool
    patch_applied: bool  # Whether the latest patch applied cleanly
    total_cost_usd: float
    # "running", "approved", "partial" (multi-task: some tasks approved, not all),
    # "exhausted", "stalled", "error"
    status: str
    error: str

    # Incremental task tracking
    _completed_tasks: list[str]  # task_ids that passed the completion gate (approved)
    _skipped_tasks: NotRequired[list[str]]  # task_ids given up on (exhausted or stalled)
    # task_id -> {"status": "approved"|"skipped"|"stalled"|"exhausted", "iterations": int}
    _task_outcomes: NotRequired[dict[str, dict[str, Any]]]
    _task_iteration: int  # attempts on the current task (resets per task)

    # Dynamic keys set during execution
    all_tasks: list[dict[str, Any]]  # all tasks from the architect plan
    execution_waves: list[list[dict[str, Any]]]  # waves of the plan's pending tasks
    _replan: bool  # signal to re-run architect before next iteration
    _replan_count: NotRequired[int]  # re-plans attempted (capped by pipeline.max_replans)
    # an orchestrator-side re-plan request ({"source", "reason"}); routes to the
    # architect under the re-plan cap and is cleared once the re-plan ran
    replan_request: NotRequired[dict[str, Any] | None]
    _iteration_base: NotRequired[int]  # iterations run before this refine round
    # global iteration the installed re-plan's budget counts from (its finished
    # tasks' share deducted), so a re-plan never inherits an already spent cap
    _plan_iteration_base: NotRequired[int]
    # the plan in ``plan`` was announced but not yet approved (interactive mode);
    # a resumed run asks again before implementing it
    _plan_pending_approval: NotRequired[bool]
    # why this session's loop stopped on a budget or limit ("" = it did not)
    _termination_reason: NotRequired[str]
    # wall time used, for the budget of a later session (resume continues the
    # round, a refine starts a new one): {"elapsed_seconds": every session's,
    # "round_base": the round's _iteration_base, "round_start_seconds": elapsed then}
    _wall_clock: NotRequired[dict[str, Any]]
    # task_id -> {"patch", "blocked_paths", "iteration"}: first patches generated in
    # parallel (parallel_tasks), used when the task becomes current
    _parallel_patches: NotRequired[dict[str, dict[str, Any]]]

    # Workspace bookkeeping, written by every patch apply
    patch_noop: NotRequired[bool]  # the latest patch changed no file content
    # path -> {"task_id", "iteration"[, "deleted": True]} of its last content change
    _file_history: NotRequired[dict[str, dict[str, Any]]]
    _last_changed_files: NotRequired[list[str]]  # paths the latest patch changed
    # pre-patch content of the latest patch's changed paths (None = did not exist)
    _pre_patch_snapshot: NotRequired[dict[str, str | None]]
    _rejected_paths: NotRequired[list[str]]  # unsafe patch paths skipped by the latest apply
    # files of the latest implementer response that were not written: [{"file", "reason"}]
    _blocked_paths: NotRequired[list[dict[str, str]]]
    workspace_commit_error: NotRequired[str]  # last failed run-branch commit ("" = committed)
    workspace_sha: NotRequired[str]  # run-branch HEAD after the latest commit
    import_issues: NotRequired[list[dict[str, Any]]]  # advisory import-validator findings

    # Current task's attempt history
    _noop_streak: NotRequired[int]  # consecutive attempts whose patch changed nothing
    _failure_signatures: NotRequired[list[str]]  # failure_signature per tested attempt ("" = pass)
    # failure excerpts of the last two tested attempts ("" = pass), for the director
    _failure_excerpts: NotRequired[list[str]]
    _agent_failures: NotRequired[dict[str, int]]  # role -> failed calls in a row, this session
    _director_guidance: NotRequired[list[dict[str, Any]]]  # REVISE rulings for the next attempt
    # conflicts already arbitrated (arbitration.conflict_key): each is ruled on once
    _arbitrated_signatures: NotRequired[list[str]]
    _arbitration_pending: NotRequired[bool]  # a ruling awaits its directed attempt

    # Security scan of the current task's changes (the latest patch included):
    # findings shown to the agents, and the subset that blocks the patch and
    # approval in "block" mode
    security_findings: NotRequired[list[dict[str, Any]]]
    _security_blockers: NotRequired[list[dict[str, Any]]]
    # distinct findings the run met: key -> {"source", "severity", "title", "file"}
    _security_detected: NotRequired[dict[str, dict[str, Any]]]

    # Test-suite history
    _accepted_test_ids: NotRequired[list[str]]  # node ids passing when a task was approved
    _accepted_test_owners: NotRequired[dict[str, str]]  # accepted node id -> approved task
    # accepted tests missing from the workspace or the latest run:
    # [{"file", "removed", "owner_task", "source": "static"|"sandbox"}]
    _removed_tests: NotRequired[list[dict[str, Any]]]
    # accepted node id -> the skipped task that removed it; left out of later tasks'
    # _removed_tests (a regression of the run) until a patch brings the test back
    _known_removals: NotRequired[dict[str, str]]
    # node id -> task that left it failing, or "pre_existing" (failing before the run)
    _known_failures: NotRequired[dict[str, str]]
    # known failures that passed later in the run: node id -> their origin
    _fixed_failures: NotRequired[dict[str, str]]
    # the pre-run baseline was taken (or found unnecessary); never repeated on resume
    _baseline_done: NotRequired[bool]
    # node id -> its latest runs [[iteration, "passed"|"failed"], ...], across tasks
    _test_history: NotRequired[dict[str, list[list[Any]]]]
    # node id -> {"test", "file", "evidence", "owner": "this_run"|"preexisting",
    # "owner_task", "detected_iteration"}: nondeterministic tests, across tasks, until
    # their file changes after the detection
    _known_flaky_tests: NotRequired[dict[str, dict[str, Any]]]
    # node id -> the first detection record of every nondeterministic test of the
    # run, never dropped; "resolved_iteration" once its file changed after it
    _flaky_seen: NotRequired[dict[str, dict[str, Any]]]
