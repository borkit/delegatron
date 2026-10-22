"""LangGraph StateGraph definition for the Delegatron pipeline.

The graph implements the core plan/code/test/review loop:

    start -> architect -> implementer -> sandbox -> reviewer -> router
    router -> "approved" -> finalize -> END
    router -> "architect" -> architect (re-plan)
    router -> "implementer" -> implementer (fix code)
    router -> "exhausted" -> finalize -> END
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import os
import posixpath
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar, cast
from urllib.parse import urlparse

from delegatron.agents.architect import ArchitectAgent
from delegatron.agents.base import (
    MIN_OUTPUT_RESERVE_TOKENS,
    AgentOutputError,
    BaseAgent,
    TruncatedOutputError,
)
from delegatron.agents.director import DirectorAgent
from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.rendering import report_excerpt
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.artifacts import ArtifactStore
from delegatron.config import ROLE_NAMES, Config, RoleConfig, is_local_model
from delegatron.constraints import Constraints, merge_constraints
from delegatron.context import (
    ContextSelection,
    gather_context,
    gather_repo_map,
    select_context,
)
from delegatron.detection import ProjectInfo, detect_project
from delegatron.factories.agent_factory import (
    create_architect,
    create_director,
    create_implementer,
    create_reviewer,
    resolve_prompt_version,
)
from delegatron.factories.sandbox_factory import create_sandbox, get_install_commands
from delegatron.factories.security_pipeline import (
    SecurityBaseline,
    SecurityResult,
    blocking_findings,
    run_security_checks,
)
from delegatron.framework_hints import build_framework_hints
from delegatron.integrations.git_pr import (
    BasePRCreator,
    GitHubPRCreator,
    build_pr_body,
    check_gh_available,
    check_git_remote,
    git_remote_url,
    remote_host,
)
from delegatron.orchestrator.arbitration import (
    MAX_FAILURE_EXCERPT,
    apply_arbitration,
    build_arbitration_input,
    conflict_key,
    detect_conflict,
    normalize_title,
)
from delegatron.orchestrator.evidence import (
    flaky_from_history,
    select_entrypoints,
    synthesize_diff,
)
from delegatron.orchestrator.fanout import build_execution_waves
from delegatron.orchestrator.observer import ConversationObserver, create_observer
from delegatron.orchestrator.plan_normalize import normalize_plan
from delegatron.orchestrator.router import architecture_issues, route_after_review
from delegatron.orchestrator.scoring import (
    can_override_rejection,
    classify_test_evidence,
    get_score_summary,
    should_accept,
)
from delegatron.orchestrator.state import AgentState
from delegatron.orchestrator.suite_guard import (
    excuse_known_failures,
    failing_test_ids,
    is_pytest_file,
    is_test_path,
    node_file,
    passing_test_ids,
    removed_tests,
    sibling_test_path,
    strip_known_tests,
)
from delegatron.orchestrator.termination import failures_recur, should_stop_for_stall
from delegatron.sandbox.base import BaseSandbox
from delegatron.sandbox.prepare import select_targeted_tests
from delegatron.sandbox.toolchains import PYTEST_COMMAND, select_docker_image
from delegatron.schemas.arbitration import ArbitrationReport
from delegatron.security.import_validator import validate_patch_imports
from delegatron.security.patch_lines import touched_paths
from delegatron.security.zero_cloud import NetworkGuard
from delegatron.utils.context_budget import CHARS_PER_TOKEN, estimate_tokens
from delegatron.utils.cost import CostTracker, get_gpu_power_watts, logged_cost_usd
from delegatron.utils.gpu_cleanup import cleanup_after_run, unload_except
from delegatron.utils.paths import normalize_rel_path
from delegatron.utils.provider import is_local_base_url
from delegatron.utils.test_output import (
    extract_test_functions,
    failure_signature,
    format_test_failures,
    remove_failure_sections,
)
from delegatron.workspace import (
    RUN_COMMIT_PREFIX,
    DirtyWorkspaceError,
    Workspace,
    WorkspaceInitError,
)
from delegatron.worktree import WorktreeManager, worktree_key

logger = logging.getLogger(__name__)

# Where ``_ensure_tests`` puts its synthetic smoke tests
SMOKE_TEST_PATH = "tests/test_smoke.py"

# ``_known_failures`` origin of a test that already failed before the run's
# first patch (the pre-run baseline, ``baseline_tests``)
PRE_EXISTING = "pre_existing"

# Run artifact holding the pre-run baseline's test report (reused on resume)
BASELINE_REPORT_ARTIFACT = "baseline_test_report.json"

# Most characters of a failed commit's git output kept in state and events
MAX_COMMIT_ERROR = 500

# git's line-ending conversion notices, which precede a failed commit's real error
_CRLF_NOTICE = re.compile(
    r"LF will be replaced by CRLF|CRLF will be replaced by LF|will have its original line endings"
)

# Byte budget for the workspace files shown to a role whose context window is
# unknown (cloud models); also the most any known window is given.
DEFAULT_FILE_BUDGET = 50_000

# With a known window: the share of the input tokens left beside the system
# prompt and schema that workspace files may take (task, feedback, test output
# and repo map share the rest), and the least file budget.
FILE_BUDGET_SHARE = 0.6
MIN_FILE_BUDGET = 2_000

# A role whose call fails this many times in a row on one task ends the run.
MAX_CONSECUTIVE_AGENT_FAILURES = 2

# Attempts on a task before it can stall: local models converge slower.
LOCAL_STALL_ATTEMPTS = 5
CLOUD_STALL_ATTEMPTS = 3

# Roles that run on another role's model configuration
_CONFIG_ROLES = {"director": "architect"}

# Audit-trail events that mark an agent step as failed (observer error count)
_FAILURE_EVENTS: dict[str, frozenset[str]] = {
    "architect": frozenset({"architect_error", "replan_failed"}),
    "implementer": frozenset({"implementer_error"}),
    "reviewer": frozenset({"reviewer_error"}),
    "director": frozenset({"arbitration_failed"}),
}

# Latest runs kept per test in ``_test_history`` (bounds checkpoint size)
TEST_HISTORY_RUNS = 5
# Tests kept in ``_test_history``; those run least recently go first
TEST_HISTORY_MAX_TESTS = 5000

# ``conflict_key`` prefix of a conflict on recurring review titles; titles churn,
# so one such conflict per task is arbitrated.
_TITLE_CONFLICT_PREFIX = "titles:"

# Labels the previous attempt's failure in the director's failing-test excerpt.
_PREVIOUS_FAILURE_LABEL = "--- previous attempt ---"

# Implementer input keys that carry feedback on an earlier attempt; any of
# them rules out two-pass generation.
_FEEDBACK_KEYS = (
    "failing_tests",
    "flaky_tests",
    "removed_tests",
    "previous_attempt",
    "director_guidance",
    "import_warnings",
    "review_feedback",
    "security_findings",
)

# Implementer input keys that describe the workspace files it is shown (or not)
_SELECTION_INPUT_KEYS = ("existing_files", "partial_files", "omitted_files", "binary_files")

# Patch files that are configuration or metadata, not source code
_CONFIG_ONLY_EXTENSIONS = (".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".md")

# Run statuses that end a multi-task run normally (vs. "error")
_SETTLED_STATUSES = frozenset({"approved", "exhausted", "stalled"})

# ``_terminate`` limits of the run's budget: a run they stop had work pending,
# so it stays "exhausted" (resumable) even when it approved some tasks.
_BUDGET_LIMITS = frozenset({"iterations", "cost", "wall_time"})

# Final run statuses the server reports to ``DELEGATRON_ALERT_WEBHOOK_URL`` (a
# "partial" run delivered only some tasks); the zero-cloud report records that
# notification as egress of the run.
ALERT_STATUSES = frozenset({"error", "exhausted", "stalled", "partial"})
ALERT_WEBHOOK_ENV = "DELEGATRON_ALERT_WEBHOOK_URL"

# The oldest implementer prompt version that explains the two-pass modes
# (``code_only`` / ``tests_only``).
TWO_PASS_PROMPT_VERSION = 4

# Events of a failed implementer call (a failure of a task generated in
# parallel is reported by ``parallel_task_fallback`` instead).
_IMPLEMENTER_FAILURE_EVENTS = frozenset({"implementer_error", "implementer_unavailable"})

_T = TypeVar("_T")

# State keys recomputed (or supplied by the caller) on every execute(), and
# those that describe one session only (why it stopped, the agent calls that
# failed in a row in it); every other AgentState key is restored from a
# checkpoint on resume and refine.
_FRESH_STATE_KEYS = frozenset(
    {
        "request",
        "project_info",
        "file_tree",
        "workspace_root",
        "status",
        "error",
        "_termination_reason",
        "_agent_failures",
    }
)
RESUME_STATE_KEYS: tuple[str, ...] = tuple(
    key for key in AgentState.__annotations__ if key not in _FRESH_STATE_KEYS
)


class _EarlyExit(Exception):
    """Ends a run before the main loop; the state already carries the reason."""


class WallTimeExceeded(Exception):
    """The run's wall-time budget (``max_wall_time_minutes``) ran out during *phase*."""

    def __init__(self, phase: str) -> None:
        super().__init__(f"Wall time limit reached during {phase}")
        self.phase = phase


@dataclass
class _WallClock:
    """The run's wall-time budget, measured across its sessions (resume, refine).

    Elapsed time adds up over every session of the run (checkpointed as
    ``_wall_clock``).  The budget counts from the start of the current round:
    a resume continues it, a refine (a new ``_iteration_base``) starts afresh.
    """

    budget_seconds: float = math.inf
    prior_seconds: float = 0.0  # elapsed in the run's earlier sessions
    round_start: float = 0.0  # elapsed when the current round began
    round_base: int = 0  # ``_iteration_base`` of the current round
    session_start: float = field(default_factory=lambda: time.monotonic())

    @classmethod
    def start(cls, saved_state: Mapping[str, Any], budget_seconds: float) -> _WallClock:
        """The clock of a session that resumes *saved_state* (empty for a fresh run)."""
        saved: Mapping[str, Any] = saved_state.get("_wall_clock") or {}
        prior = float(saved.get("elapsed_seconds") or 0.0)
        round_base = int(saved_state.get("_iteration_base") or 0)
        same_round = saved.get("round_base") == round_base
        round_start = float(saved.get("round_start_seconds") or 0.0) if same_round else prior
        return cls(budget_seconds, prior, round_start, round_base)

    def session_seconds(self) -> float:
        """Seconds since this session started."""
        return time.monotonic() - self.session_start

    def elapsed(self) -> float:
        """Seconds the run has taken, over all its sessions."""
        return self.prior_seconds + self.session_seconds()

    def remaining(self) -> float:
        """Seconds left of the current round's budget (negative once it ran out)."""
        return self.budget_seconds - (self.elapsed() - self.round_start)

    def checkpoint(self) -> dict[str, Any]:
        """The ``_wall_clock`` state entry a later session resumes from."""
        return {
            "elapsed_seconds": round(self.elapsed(), 3),
            "round_base": self.round_base,
            "round_start_seconds": round(self.round_start, 3),
        }


class _SiblingCallFailed(RuntimeError):
    """The implementer call for a task generated in parallel failed (it runs live later)."""


class _EventSink(Protocol):
    """Where audit events go besides the state: ``events.jsonl`` (an ``ArtifactStore``)."""

    def append_event(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class _SiblingEvents:
    """Event sink of a task's first patch generated in parallel with the current task.

    Every event is tagged with that task and ``parallel: true`` before it reaches
    the run's store, so it is never read as the current task's.  Its call
    failure is left out: ``parallel_task_fallback`` reports it.
    """

    store: _EventSink
    task_id: str

    def append_event(self, event: dict[str, Any]) -> None:
        if event.get("event") in _IMPLEMENTER_FAILURE_EVENTS:
            return
        self.store.append_event(self.tag(event))

    def tag(self, event: dict[str, Any]) -> dict[str, Any]:
        """*event* as this sink writes it: tagged with its task and ``parallel: true``."""
        return {**event, "task_id": self.task_id, "parallel": True}


@dataclass(frozen=True)
class _StepMark:
    """The cost tracker's totals and the audit trail's length when an agent step began."""

    input_tokens: int
    output_tokens: int
    calls: int
    messages: int

    @classmethod
    def take(cls, cost_tracker: CostTracker, state: AgentState) -> _StepMark:
        return cls(*cost_tracker.usage_mark(), len(state["messages"]))

    def usage(self, cost_tracker: CostTracker) -> dict[str, int]:
        """Provider calls and tokens booked since the mark (observer keywords)."""
        input_tokens, output_tokens, calls = cost_tracker.usage_mark()
        return {
            "calls": calls - self.calls,
            "input_tokens": input_tokens - self.input_tokens,
            "output_tokens": output_tokens - self.output_tokens,
        }


def _new_state(request: str, workspace_root: Path) -> AgentState:
    """Initial state of a run (project_info and file_tree are filled in later)."""
    return {
        "messages": [],
        "request": request,
        "project_info": {},
        "file_tree": [],
        "workspace_root": str(workspace_root),
        "plan": None,
        "current_task": None,
        "current_patch": None,
        "test_report": None,
        "review_report": None,
        "previous_review": None,
        "iteration": 0,
        "is_approved": False,
        "patch_applied": False,
        "total_cost_usd": 0.0,
        "status": "running",
        "error": "",
        "_completed_tasks": [],
        "_skipped_tasks": [],
        "_task_outcomes": {},
        "_task_iteration": 0,
        "all_tasks": [],
        "execution_waves": [],
        "_replan": False,
        "_replan_count": 0,
        "replan_request": None,
        "_plan_iteration_base": 0,
        "_plan_pending_approval": False,
        "_termination_reason": "",
        "_parallel_patches": {},
        "patch_noop": False,
        "_file_history": {},
        "_last_changed_files": [],
        "_pre_patch_snapshot": {},
        "_rejected_paths": [],
        "_blocked_paths": [],
        "workspace_commit_error": "",
        "workspace_sha": "",
        "import_issues": [],
        "_noop_streak": 0,
        "_failure_signatures": [],
        "_failure_excerpts": [],
        "_agent_failures": {},
        "_director_guidance": [],
        "_arbitrated_signatures": [],
        "_arbitration_pending": False,
        "security_findings": [],
        "_security_blockers": [],
        "_accepted_test_ids": [],
        "_accepted_test_owners": {},
        "_removed_tests": [],
        "_known_removals": {},
        "_known_failures": {},
        "_fixed_failures": {},
        "_baseline_done": False,
        "_test_history": {},
        "_known_flaky_tests": {},
        "_flaky_seen": {},
        "_security_detected": {},
        "_iteration_base": 0,
        "_wall_clock": {},
    }


def _project_info_dict(project_info: ProjectInfo) -> dict[str, Any]:
    return {
        "languages": project_info.languages,
        "frameworks": project_info.frameworks,
        "test_commands": project_info.test_commands,
        "lint_commands": project_info.lint_commands,
        "package_manager": project_info.package_manager,
    }


def _init_task_plan(
    state: AgentState, tasks: list[dict[str, Any]], *, keep_current: bool = False
) -> None:
    """Set ``all_tasks``/``execution_waves`` from a plan's tasks and pick the current task.

    The waves cover the tasks not finished yet.  With *keep_current* the
    current task stays when it is still in the plan; otherwise it becomes the
    next pending task (see ``_next_pending_task``).
    """
    finished = _finished_task_ids(state)
    state["all_tasks"] = tasks
    state["execution_waves"] = build_execution_waves(
        [task for task in tasks if task.get("task_id") not in finished], done=finished
    )
    current_id = (state.get("current_task") or {}).get("task_id")
    if keep_current and any(t.get("task_id") == current_id for t in tasks):
        return
    state["current_task"] = _next_pending_task(tasks, finished) or tasks[0]


def _finished_task_ids(state: Mapping[str, Any]) -> set[str]:
    """Ids of the tasks that are approved or were given up."""
    return {*(state.get("_completed_tasks") or []), *(state.get("_skipped_tasks") or [])}


def _replan_finished_ids(
    state: Mapping[str, Any], tasks: Iterable[Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    """The completed and the skipped task ids that a re-plan's *tasks* keep finished.

    A completed task stays finished when the re-plan keeps its id; a skipped
    one only when the re-plan keeps it unchanged (same title and outputs): one
    reworked under its old id is to be done again.
    """
    new = {task.get("task_id", ""): task for task in tasks}
    old = {task.get("task_id", ""): task for task in state.get("all_tasks") or []}
    completed = [i for i in state.get("_completed_tasks") or [] if i in new]
    skipped = [
        i for i in state.get("_skipped_tasks") or [] if i in new and _same_task(old.get(i), new[i])
    ]
    return completed, skipped


def _same_task(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    """Whether a re-plan's task *new* is the plan's task *old* (title and outputs)."""
    return (
        old is not None
        and old.get("title") == new.get("title")
        and sorted(old.get("outputs") or []) == sorted(new.get("outputs") or [])
    )


def _ready_tasks(tasks: list[dict[str, Any]], finished: Collection[str]) -> list[dict[str, Any]]:
    """The tasks not yet finished whose dependencies are all finished, in plan order.

    Dependencies naming no task of the plan are ignored.
    """
    plan_ids = {task.get("task_id") for task in tasks}
    return [
        task
        for task in tasks
        if task.get("task_id") not in finished
        and all(dep in finished for dep in task.get("depends_on") or [] if dep in plan_ids)
    ]


def _next_pending_task(
    tasks: list[dict[str, Any]], finished: Collection[str]
) -> dict[str, Any] | None:
    """The first task in plan order not yet finished whose dependencies are all finished.

    When no pending task is ready (a dependency cycle), the first pending task
    is returned.  ``None`` when every task is finished.
    """
    ready = _ready_tasks(tasks, finished)
    if ready:
        return ready[0]
    return next((task for task in tasks if task.get("task_id") not in finished), None)


def _iteration_cap(state: Mapping[str, Any], max_iterations: int) -> int:
    """The run's global iteration cap: *max_iterations* for each task of the plan.

    Counted from the start of a refine round (``_iteration_base``) or, after a
    re-plan, from where that plan's budget starts (``_plan_iteration_base``),
    whichever is later.
    """
    base = max(int(state.get("_iteration_base") or 0), int(state.get("_plan_iteration_base") or 0))
    return base + max_iterations * max(len(state.get("all_tasks") or []), 1)


def task_outcome_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-task outcomes in plan order: ``{task_id, title, status, iterations}``."""
    outcomes: Mapping[str, Mapping[str, Any]] = state.get("_task_outcomes") or {}
    return [
        {"task_id": task_id, "title": task.get("title", ""), **outcomes[task_id]}
        for task in state.get("all_tasks") or []
        if (task_id := task.get("task_id", "")) in outcomes
    ]


def _close_task_outcomes(state: AgentState) -> None:
    """Give every planned task an outcome once the run has ended.

    Approved tasks of older checkpoints count as approved; the current task
    ends as the run did ("exhausted"/"stalled"); any other task was "skipped".
    """
    outcomes = state.setdefault("_task_outcomes", {})
    completed = set(state.get("_completed_tasks") or [])
    current_id = (state.get("current_task") or {}).get("task_id")
    for task in state.get("all_tasks") or []:
        task_id = task.get("task_id", "")
        if task_id in outcomes:
            continue
        is_current = task_id == current_id
        if task_id in completed:
            status = "approved"
        elif is_current and state["status"] in ("exhausted", "stalled"):
            status = state["status"]
        else:
            status = "skipped"
        iterations = state.get("_task_iteration", 0) if is_current else 0
        outcomes[task_id] = {"status": status, "iterations": iterations}


def _settle_run_status(state: AgentState, *, budget_stop: bool = False) -> None:
    """Report a run that ended normally as "partial" when not everything was delivered.

    That is a multi-task run that approved some, but not all, of its tasks, or
    an approved run while an accepted test of an approved task still fails or
    is gone (``_regressed_tests``): such a run is never approved, so it opens
    no PR.  A run a budget limit stopped (*budget_stop*: iterations, cost,
    wall time) keeps its "exhausted" status: work was still pending, and a
    resume continues it.
    """
    task_ids = {task.get("task_id", "") for task in state.get("all_tasks") or []}
    approved = task_ids & set(state.get("_completed_tasks") or [])
    some_approved = (
        not budget_stop and len(task_ids) > 1 and bool(approved) and approved != task_ids
    )
    regressed = state["status"] == "approved" and bool(_regressed_tests(state))
    if state["status"] in _SETTLED_STATUSES and (some_approved or regressed):
        state["status"] = "partial"
        state["is_approved"] = False


def _regressed_tests(state: Mapping[str, Any]) -> dict[str, str]:
    """Accepted tests (of approved tasks) that now fail or are gone: ``{node id: skipped task}``.

    A skipped task's failing tests are excused for later tasks
    (``_known_failures``), even when it broke a test an approved task left
    passing, and so are the accepted tests it removed (``_known_removals``);
    those are regressions of the run.
    """
    accepted = set(state.get("_accepted_test_ids") or [])
    known: Mapping[str, str] = state.get("_known_failures") or {}
    regressed = {nodeid: task for nodeid, task in known.items() if nodeid in accepted}
    regressed.update(state.get("_known_removals") or {})
    return dict(sorted(regressed.items()))


def _accepted_owner(state: Mapping[str, Any], nodeid: str) -> str | None:
    """The approved task an accepted test belongs to (None when unknown).

    Checkpoints older than ``_accepted_test_owners`` fall back to the task that
    last wrote the test's file, when that task was approved.
    """
    owner = (state.get("_accepted_test_owners") or {}).get(nodeid)
    if owner is None:
        owner = ((state.get("_file_history") or {}).get(node_file(nodeid)) or {}).get("task_id")
        if owner not in (state.get("_completed_tasks") or []):
            return None
    return str(owner)


def _mark_regressions(state: AgentState, regressed: Mapping[str, str]) -> None:
    """Note each regressed test on the outcome of the approved task it belongs to."""
    outcomes = state.get("_task_outcomes") or {}
    for nodeid in regressed:
        owner = _accepted_owner(state, nodeid)
        if owner in outcomes:
            tests = outcomes[owner].setdefault("regressed_tests", [])
            if nodeid not in tests:
                tests.append(nodeid)


def _read_workspace_files(root: Path, paths: Iterable[str]) -> dict[str, str | None]:
    """Current text of each workspace-relative path (``None`` when absent or unsafe)."""
    texts: dict[str, str | None] = {}
    for path in paths:
        rel = normalize_rel_path(path)
        target = root / rel if rel else None
        texts[path] = (
            target.read_text(encoding="utf-8", errors="replace")
            if target is not None and target.is_file()
            else None
        )
    return texts


def _merge_two_pass(
    pass1_files: Mapping[str, str], pass2_files: Mapping[str, str], workspace_root: Path
) -> tuple[dict[str, str], list[str]]:
    """Combine a code pass and a tests pass into one file map.

    Pass 1 contributes its source files; pass 2 its test files (any language)
    and new support files, e.g. fixtures or helpers, that neither pass 1 nor the
    workspace already has.  When pass 2 wrote no test file, pass 1's test files
    are used.  Returns the merged files and the pass-2 paths that were dropped
    (rewrites of pass-1 code or of existing workspace files).
    """
    merged = {path: content for path, content in pass1_files.items() if not is_test_path(path)}
    tests: dict[str, str] = {}
    dropped: list[str] = []
    for path, content in pass2_files.items():
        rel = normalize_rel_path(path)
        is_new = path not in merged and rel is not None and not (workspace_root / rel).exists()
        if is_test_path(path) or is_new:
            tests[path] = content
        else:
            dropped.append(path)
    if not any(is_test_path(path) for path in tests):
        tests.update({path: c for path, c in pass1_files.items() if is_test_path(path)})
    return {**merged, **tests}, sorted(dropped)


def _is_source_path(path: str) -> bool:
    """Whether a patch path is source code rather than configuration or metadata."""
    return (
        not path.endswith(_CONFIG_ONLY_EXTENSIONS)
        and "__init__" not in path
        and "conftest" not in path
    )


def _restore_state(state: AgentState, saved: Mapping[str, Any]) -> None:
    """Copy a checkpoint's ``RESUME_STATE_KEYS`` into a fresh state.

    Checkpoints written before ``all_tasks`` was saved get the task list rebuilt
    from the plan, keeping the saved current task; so do checkpoints whose
    plan predates plan normalization (``_normalize_restored_plan``).
    """
    target = cast(dict[str, Any], state)
    for key in RESUME_STATE_KEYS:
        if key in saved:
            target[key] = saved[key]
    if _normalize_restored_plan(state):
        return
    tasks = (state.get("plan") or {}).get("tasks") or []
    if tasks and not state.get("all_tasks"):
        _init_task_plan(state, tasks, keep_current=True)


def _normalize_restored_plan(state: AgentState) -> bool:
    """Normalize a restored plan whose task ids are not unique and non-empty.

    Such a plan comes from a checkpoint written before ``normalize_plan`` ran
    on every plan: a duplicate id would count as finished with its twin.  No
    task is merged (finished tasks stay what they were); the current task
    keeps its position in the plan.  Returns whether the plan was rebuilt.
    """
    plan = state.get("plan") or {}
    tasks: list[dict[str, Any]] = plan.get("tasks") or []
    ids = [task.get("task_id") for task in tasks]
    if not tasks or (all(ids) and len(set(ids)) == len(ids)):
        return False
    position = next((i for i, task in enumerate(tasks) if task == state.get("current_task")), 0)
    normalized, warnings = normalize_plan(plan, state.get("file_tree") or [], merge_redundant=False)
    logger.warning("Restored plan normalized: %s", "; ".join(warnings))
    new_tasks: list[dict[str, Any]] = normalized.get("tasks") or []
    state["plan"] = normalized
    state["current_task"] = new_tasks[min(position, len(new_tasks) - 1)]
    _init_task_plan(state, new_tasks, keep_current=True)
    return True


def _reset_for_next_task(state: AgentState, next_task: dict[str, Any]) -> None:
    """Make *next_task* current and drop everything specific to the previous task."""
    state["current_task"] = next_task
    state["is_approved"] = False
    state["review_report"] = None
    state["previous_review"] = None
    state["test_report"] = None
    state["_task_iteration"] = 0
    state["import_issues"] = []
    state["_rejected_paths"] = []
    state["_blocked_paths"] = []
    state["_noop_streak"] = 0
    state["_failure_signatures"] = []
    state["_failure_excerpts"] = []
    state["_agent_failures"] = {}
    state["_director_guidance"] = []
    state["_arbitrated_signatures"] = []
    state["_arbitration_pending"] = False
    state["security_findings"] = []
    state["_security_blockers"] = []
    state["_removed_tests"] = []
    # A re-plan asked for by the previous task (by its review or the director)
    # never carries over to this one.
    state["_replan"] = False
    state["replan_request"] = None


def restart_current_task(state: AgentState) -> None:
    """Re-open the current task for another round (a resume of a task given up, a refine).

    The task is re-opened (``_reopen_task``) and its attempt history is reset
    as for a new task (``_reset_for_next_task``).  Its latest review is kept:
    it is feedback on this very task (a refine puts the user's there).
    """
    current = state.get("current_task")
    if not current:
        return
    _reopen_task(state, current.get("task_id", ""))
    review = state.get("review_report")
    _reset_for_next_task(state, current)
    state["review_report"] = review


def _reopen_task(state: AgentState, task_id: str) -> None:
    """Make a finished task pending again, as if it had never been attempted.

    It leaves ``_completed_tasks`` and ``_skipped_tasks`` and loses its outcome.
    What it left behind stops being excused (its known failures and removals)
    or protected (the tests it accepted, recorded again when it is approved
    again): the task's next round is judged on its own.
    """
    accepted = state.get("_accepted_test_ids") or []
    own_tests = {nodeid for nodeid in accepted if _accepted_owner(state, nodeid) == task_id}
    if own_tests:
        state["_accepted_test_ids"] = [nodeid for nodeid in accepted if nodeid not in own_tests]
        owners = state.get("_accepted_test_owners") or {}
        for nodeid in own_tests:
            owners.pop(nodeid, None)
    for key in ("_completed_tasks", "_skipped_tasks"):
        _remove_task_id(state, key, task_id)
    (state.get("_task_outcomes") or {}).pop(task_id, None)
    for key in ("_known_failures", "_known_removals"):
        left: dict[str, str] = cast(dict[str, Any], state).get(key) or {}
        for nodeid in [nodeid for nodeid, owner in left.items() if owner == task_id]:
            del left[nodeid]


def _task_passed(state: AgentState) -> bool:
    """The completion gate, the same for every task of a plan.

    The patch is approved (reviewer acceptance under the hard gates, or the
    EMCS override) and no accepted test of an earlier task went missing.
    """
    return bool(state.get("is_approved")) and not state.get("_removed_tests")


def _block_regressions(state: AgentState) -> None:
    """Accepted tests that went missing veto any approval (a ruling or a restored one too)."""
    if state.get("_removed_tests"):
        state["is_approved"] = False


def _add_task_id(state: AgentState, key: str, task_id: str) -> None:
    """Append *task_id* to the task-id list at *key* unless it is already there."""
    target = cast(dict[str, Any], state)
    ids: list[str] = target.get(key) or []
    if task_id not in ids:
        target[key] = [*ids, task_id]


def _remove_task_id(state: AgentState, key: str, task_id: str) -> None:
    """Drop *task_id* from the task-id list at *key*."""
    target = cast(dict[str, Any], state)
    ids: list[str] = target.get(key) or []
    if task_id in ids:
        target[key] = [i for i in ids if i != task_id]


def _record_known_failures(state: AgentState, task_id: str) -> list[str]:
    """Remember the tests a skipped task left failing, so they stop blocking later tasks.

    Returns the failing test ids (empty when the failure names no test, and
    when the task's last run failed nothing: it passed or tested nothing).
    """
    report = state.get("test_report")
    if report is None or classify_test_evidence(report) != "failed":
        return []
    ids = failing_test_ids(report)
    if ids is None:
        logger.warning("Task %s ended with a failure that names no test", task_id)
        return []
    known = state.setdefault("_known_failures", {})
    for nodeid in ids:
        known.setdefault(nodeid, task_id)
    return sorted(ids)


def _record_known_removals(state: AgentState, task_id: str) -> list[str]:
    """Remember the accepted tests a skipped task left removed, so they stop blocking later tasks.

    Its last pass's ``_removed_tests`` (the removal stays on the run branch).
    Returns their node ids.
    """
    removed = sorted(
        f"{item['file']}::{name}"
        for item in state.get("_removed_tests") or []
        for name in item.get("removed") or []
    )
    known = state.setdefault("_known_removals", {})
    for nodeid in removed:
        known.setdefault(nodeid, task_id)
    return removed


def _test_commands(state: Mapping[str, Any]) -> list[str]:
    """The test commands the sandbox runs for the project as detected now."""
    return list((state.get("project_info") or {}).get("test_commands") or [PYTEST_COMMAND])


def _known_failure_origins(state: Mapping[str, Any], ids: Iterable[str]) -> dict[str, str]:
    """``{node id: origin}`` of known failures *ids*.

    The origin is the task that left the test failing, or ``PRE_EXISTING``
    when it already failed before the run.
    """
    known: Mapping[str, str] = state.get("_known_failures") or {}
    return {nodeid: known.get(nodeid, PRE_EXISTING) for nodeid in ids}


def _prior_files(state: Mapping[str, Any]) -> list[str]:
    """Files earlier work left for the current task to build on, most relevant first.

    From ``_file_history`` (deleted files excluded): the current task's own
    files, then those of its dependencies (transitively, nearest first), then
    those of the other completed tasks.  A checkpoint without file history
    falls back to the plan outputs of the dependencies and completed tasks.
    """
    current = state.get("current_task") or {}
    current_id = current.get("task_id", "")
    tasks = state.get("all_tasks") or (state.get("plan") or {}).get("tasks") or []
    by_id: dict[str, dict[str, Any]] = {t.get("task_id", ""): t for t in tasks}
    owners = dict.fromkeys(
        [current_id, *_dependencies(current, by_id), *(state.get("_completed_tasks") or [])]
    )
    history: Mapping[str, Mapping[str, Any]] = state.get("_file_history") or {}
    files: list[str] = []
    for task_id in owners:
        if history:
            files += _files_of_task(history, task_id)
        elif task_id != current_id:
            files += by_id.get(task_id, {}).get("outputs") or []
    return list(dict.fromkeys(files))


def _files_of_task(history: Mapping[str, Mapping[str, Any]], task_id: str) -> list[str]:
    """The files *task_id* last wrote that still exist, according to ``_file_history``."""
    return sorted(
        path
        for path, entry in history.items()
        if entry.get("task_id") == task_id and not entry.get("deleted")
    )


def _skipped_tasks_input(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The tasks given up, for a re-plan: id, title, outputs, outcome ``status`` and ``reason``.

    A re-plan that keeps one unchanged leaves it given up; one that changes
    it has it done again (``_replan_finished_ids``).
    """
    skipped = set(state.get("_skipped_tasks") or [])
    outcomes: Mapping[str, Mapping[str, Any]] = state.get("_task_outcomes") or {}
    tasks: list[dict[str, Any]] = []
    for task in state.get("all_tasks") or []:
        task_id = task.get("task_id", "")
        if task_id not in skipped:
            continue
        outcome = outcomes.get(task_id) or {}
        entry: dict[str, Any] = {
            "task_id": task_id,
            "title": task.get("title", ""),
            "outputs": task.get("outputs") or [],
            "status": outcome.get("status", "exhausted"),
        }
        if outcome.get("reason"):
            entry["reason"] = outcome["reason"]
        tasks.append(entry)
    return tasks


def _task_files(state: Mapping[str, Any]) -> list[str]:
    """The files the current task wrote that still exist."""
    task_id = (state.get("current_task") or {}).get("task_id", "")
    return _files_of_task(state.get("_file_history") or {}, task_id)


def _dependencies(task: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Ids of the plan tasks *task* depends on, transitively, nearest first."""
    found: list[str] = []
    pending = list(task.get("depends_on") or [])
    while pending:
        dep = pending.pop(0)
        if dep in by_id and dep not in found:
            found.append(dep)
            pending.extend(by_id[dep].get("depends_on") or [])
    return found


def _test_outcomes(report: Mapping[str, Any] | None) -> dict[str, bool]:
    """``{node id: passed}`` of a run: passing per-test results and failing ids."""
    outcomes = dict.fromkeys(passing_test_ids(report), True)
    outcomes.update(dict.fromkeys(failing_test_ids(report) or (), False))
    return outcomes


def _task_failure_signature(report: Mapping[str, Any], known_flaky: Collection[str] = ()) -> str:
    """``failure_signature`` of the task's own failures ("" when none).

    Flaky tests (this run's and the *known_flaky* ones) and the known failures
    of skipped tasks are left out, and a skipped sandbox gives no failure
    signal, so a signature repeats only when the task's own failure does.
    """
    if report.get("skipped_reason") or report.get("task_passed", report.get("passed")):
        return ""
    ids: list[str] = report.get("new_failures") or report.get("failed_tests") or []
    flaky = {*(report.get("flaky_tests") or []), *known_flaky}
    own = [nodeid for nodeid in ids if nodeid not in flaky]
    if ids and not own:
        return ""
    return failure_signature({**report, "failed_tests": own})


def _merge_test_history(
    history: dict[str, list[list[Any]]], outcomes: Mapping[str, bool], iteration: int
) -> None:
    """Fold one run's ``{node id: passed}`` into ``_test_history`` (in place).

    Each test keeps its latest ``TEST_HISTORY_RUNS`` runs, oldest first; a test
    absent from the run keeps its entry, so its last iteration tells it was not
    run.  Merging the same iteration again changes nothing.  At most
    ``TEST_HISTORY_MAX_TESTS`` tests are kept: those run least recently go.
    """
    for nodeid, passed in outcomes.items():
        runs = history.setdefault(nodeid, [])
        if runs and runs[-1][0] == iteration:
            continue
        runs.append([iteration, "passed" if passed else "failed"])
        del runs[:-TEST_HISTORY_RUNS]
    if len(history) > TEST_HISTORY_MAX_TESTS:
        stale = sorted(
            history, key=lambda nodeid: history[nodeid][-1][0] if history[nodeid] else -1
        )
        for nodeid in stale[: len(history) - TEST_HISTORY_MAX_TESTS]:
            del history[nodeid]


def _flaky_record(
    nodeid: str, evidence: str, file_history: Mapping[str, Mapping[str, Any]], iteration: int
) -> dict[str, Any]:
    """A nondeterministic test, attributed: written by this run (``_file_history``) or not."""
    file = node_file(nodeid)
    owner_task = (file_history.get(file) or {}).get("task_id")
    return {
        "test": nodeid,
        "file": file,
        "evidence": evidence,
        "owner": "this_run" if owner_task is not None else "preexisting",
        "owner_task": owner_task,
        "detected_iteration": iteration,
    }


def _cites_passing_test(issue: Mapping[str, Any], failing_files: Collection[str]) -> bool:
    """Whether a review issue's evidence is a test file none of whose tests fails now.

    The evidence's first word is read as a path with an optional ``:line`` or
    ``::test`` suffix (``./tests/x.py:33``, ``tests/x.py line 33``); a bare
    file name matches a failing file of that name in any directory.
    """
    words = str(issue.get("evidence") or "").replace("\\", "/").split()
    token = words[0].lstrip("`'\"([").partition(":")[0].rstrip("`'\")],;.") if words else ""
    path = normalize_rel_path(token) if token else None
    if path is None or not is_test_path(path):
        return False
    if "/" in path:
        return path not in failing_files
    return all(posixpath.basename(file) != path for file in failing_files)


def _prior_cost_usd(initial_state: Mapping[str, Any] | None, call_log: Path) -> float:
    """Spend of earlier sessions of this run: checkpoint total or call log, the larger."""
    if initial_state is None:
        return 0.0
    return max(float(initial_state.get("total_cost_usd") or 0.0), logged_cost_usd(call_log))


def _emit_event(state: AgentState, store: _EventSink | None, event: dict[str, Any]) -> None:
    """Record *event* in the state's audit trail and in ``events.jsonl``."""
    state["messages"].append(event)
    if store is not None:
        store.append_event(dict(event))


def _save_json_artifact(
    store: ArtifactStore | None, filename: str, data: Mapping[str, Any]
) -> None:
    """Write a run artifact that has no ``ArtifactStore`` method of its own (as the store does)."""
    if store is None:
        return
    path = store.run_dir / filename
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logger.debug("Saved artifact: %s", path)


def _load_json_artifact(store: ArtifactStore, filename: str) -> dict[str, Any] | None:
    """A JSON object artifact saved by ``_save_json_artifact`` (None when absent or unreadable)."""
    path = store.run_dir / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable artifact %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _trim_report(trimmed: Any) -> dict[str, Any]:
    """The context guard's ``_meta["context_trimmed"]`` as ``{"keys", ...}``.

    The guard reports the trimmed input keys, either as a list or as a dict
    that also carries ``window``, ``tokens_before``, ``tokens_after``,
    ``dropped`` (paths, or ``{input key: paths}``) and ``still_over``.
    """
    if isinstance(trimmed, Mapping):
        report = {key: value for key, value in trimmed.items() if value is not None}
        report["keys"] = list(trimmed.get("keys") or trimmed.get("trimmed") or [])
        return report
    return {"keys": list(trimmed or [])}


def _dropped_paths(report: Mapping[str, Any]) -> list[str]:
    """The workspace files a trim report says the guard dropped from the prompt."""
    dropped = report.get("dropped")
    if isinstance(dropped, Mapping):
        dropped = [path for paths in dropped.values() for path in paths or []]
    return [str(path) for path in dropped or []] if isinstance(dropped, list) else []


def _same_sandbox(spec: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Whether two ``create_sandbox`` specs build the same sandbox.

    No language ("") is the default sandbox, which is Python's.
    """

    def normalized(s: Mapping[str, Any]) -> dict[str, Any]:
        return {**s, "primary_language": s.get("primary_language") or "python"}

    return normalized(spec) == normalized(other)


def _excerpted_paths(report: Mapping[str, Any]) -> list[str]:
    """The workspace files a trim report says the guard shortened to excerpts."""
    excerpted = report.get("excerpted")
    if isinstance(excerpted, Mapping):
        excerpted = [path for paths in excerpted.values() for path in paths or []]
    return [str(path) for path in excerpted or []] if isinstance(excerpted, list) else []


def _mark_excerpted(selection: ContextSelection, paths: Iterable[str]) -> None:
    """Count the shown files the context guard cut to excerpts as shown only in part.

    The attempt never saw them whole, so a whole-file rewrite of one is
    refused like that of any excerpt (``partial``: the on-disk size).
    """
    for path in paths:
        text = selection.files.get(path)
        if text is not None and path not in selection.partial:
            selection.partial[path] = len(text.encode("utf-8"))


def _omit_dropped(selection: ContextSelection, paths: Iterable[str]) -> None:
    """Count the shown files the context guard dropped from the prompt as left out (``omitted``)."""
    for path in paths:
        text = selection.files.pop(path, None)
        if text is not None:
            selection.omitted[path] = selection.partial.pop(path, len(text.encode("utf-8")))


def _prompt_overhead_tokens(agent: object) -> int:
    """Tokens an agent's every call spends on its system prompt and output schema.

    ``BaseAgent.prompt_overhead_tokens``; for other agents (test doubles) read
    the way ``BaseAgent`` budgets them, 0 when they have neither.
    """
    if isinstance(agent, BaseAgent):
        return agent.prompt_overhead_tokens()
    system_prompt = getattr(agent, "_system_prompt", "")
    output_schema = getattr(agent, "_get_output_schema", None)
    tokens = estimate_tokens(system_prompt) if isinstance(system_prompt, str) else 0
    if callable(output_schema):
        tokens += estimate_tokens(json.dumps(output_schema(), indent=2))
    return tokens


def _fit_files(
    sources: Iterable[Mapping[str, str]], budget: int, keep: Collection[str] = ()
) -> dict[str, str]:
    """Files from *sources*, in order (the first source's content wins), within *budget* bytes.

    The *keep* files are always included; the others while they fit.
    """
    fitted: dict[str, str] = {}
    used = 0
    for files in sources:
        for path, text in files.items():
            if path in fitted:
                continue
            size = len(text.encode("utf-8"))
            if path in keep or used + size <= budget:
                fitted[path] = text
                used += size
    return fitted


def _selection_input(selection: ContextSelection) -> dict[str, Any]:
    """The implementer input that describes *selection* (``_SELECTION_INPUT_KEYS``).

    The files shown (``existing_files``), the on-disk size of those shown
    only as an excerpt (``partial_files``), and the relevant files not shown
    (``omitted_files``, ``binary_files``).
    """
    return {
        "existing_files": selection.files,
        "partial_files": selection.partial,
        "omitted_files": selection.omitted,
        "binary_files": selection.binary,
    }


def _protected_files(
    state: Mapping[str, Any], feedback: Iterable[Mapping[str, Any]], selection: ContextSelection
) -> list[str]:
    """The shown files the attempt must edit, which the context guard must not drop.

    The task's outputs, the files of the failing tests, of the feedback items
    and (on a retry) of the last patch.
    """
    task = state.get("current_task") or {}
    report = state.get("test_report")
    failing = failing_test_ids(report) if classify_test_evidence(report) == "failed" else None
    patch = (state.get("current_patch") or {}) if state.get("_task_iteration") else {}
    named = {
        *(task.get("outputs") or []),
        *(node_file(nodeid) for nodeid in failing or ()),
        *(str(item["file"]) for item in feedback if item.get("file")),
        *(patch.get("files") or {}),
    }
    paths = {path for name in named if (path := normalize_rel_path(str(name)))}
    return [path for path in selection.files if path in paths]


def _may_arbitrate(key: str, arbitrated: Collection[str]) -> bool:
    """Whether a conflict (``conflict_key``) may still go to the director on this task.

    Each recurring failure once; recurring review titles once in all, since
    their combination churns from attempt to attempt.
    """
    if key in arbitrated:
        return False
    is_titles = key.startswith(_TITLE_CONFLICT_PREFIX)
    return not (is_titles and any(k.startswith(_TITLE_CONFLICT_PREFIX) for k in arbitrated))


def _with_previous_failure(current: str, previous: str) -> str:
    """The director's failing-test excerpt: this attempt's, then the previous attempt's.

    Together they stay within ``MAX_FAILURE_EXCERPT`` characters (two thirds
    for the current failure).
    """
    budget = MAX_FAILURE_EXCERPT - len(_PREVIOUS_FAILURE_LABEL) - 2
    now = format_test_failures(current, max_chars=budget * 2 // 3)
    before = format_test_failures(previous, max_chars=budget - len(now))
    return f"{now}\n{_PREVIOUS_FAILURE_LABEL}\n{before}"


def _failing_tests_text(report: Mapping[str, Any]) -> str:
    """What the implementer reads of a failed run: its failure excerpt, else its counts.

    Known failures (``baseline_failures``) are left out of the excerpt: they
    reach the implementer separately, with their origin.  A runner that
    failed silently (no output, no excerpt) still shows that, and how many,
    tests failed.
    """
    excerpt = _new_failures_excerpt(report)
    if excerpt.strip():
        return excerpt
    facts = {key: report.get(key) for key in ("passed", "total", "failures", "failed_tests")}
    if report.get("baseline_failures"):
        facts["failed_tests"] = report.get("new_failures")
    return json.dumps({key: value for key, value in facts.items() if value is not None}, indent=2)


def _new_failures_excerpt(report: Mapping[str, Any]) -> str:
    """``report_excerpt`` without the failures of known-failing tests (``baseline_failures``)."""
    return remove_failure_sections(
        report_excerpt(report),
        report.get("baseline_failures") or (),
        keep=report.get("new_failures") or (),
    )


def _without_known_failures(report: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of *report* whose failure excerpt leaves out the known failures.

    With no other failure left to show, its raw output is left out too (it
    would only repeat them).
    """
    excerpt = _new_failures_excerpt(report)
    if excerpt.strip():
        return {**report, "failure_excerpt": excerpt}
    return {**report, "failure_excerpt": "", "stdout": "", "stderr": ""}


def _commit_error_text(error: Exception) -> str:
    """A failed commit's error for state and events: at most ``MAX_COMMIT_ERROR`` characters.

    git's line-ending notices are left out (Git for Windows prints two per
    file before the real cause), and a long error keeps its first line and
    its tail, where git and the hooks print the cause.
    """
    lines = [line for line in str(error).splitlines() if not _CRLF_NOTICE.search(line)]
    text = "\n".join(lines).strip()
    if len(text) <= MAX_COMMIT_ERROR:
        return text
    head = lines[0][: MAX_COMMIT_ERROR // 4]
    return f"{head}\n...\n{text[-(MAX_COMMIT_ERROR - len(head) - 5) :]}"


def _patch_paths(patch: Mapping[str, Any]) -> list[str]:
    """The paths a patch writes, deletes or diffs (diff paths from its file headers)."""
    paths = [*(patch.get("files") or {}), *(patch.get("delete_files") or [])]
    lines = str(patch.get("diff_unified") or "").splitlines()
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        # A "---" line is a header only right before its "+++" line.
        if line.startswith("+++ ") or (line.startswith("--- ") and following.startswith("+++ ")):
            target = line[4:].split("\t", 1)[0].strip()
            if target != "/dev/null":
                paths.append(target[2:] if target.startswith(("a/", "b/")) else target)
    return list(dict.fromkeys(paths))


def _has_control_chars(text: str) -> bool:
    """Whether *text* holds a line break, NUL or another control character."""
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def _entrypoint_summary(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each entrypoint smoke run in brief: its file, exit code, timeout and whether it printed."""
    return [
        {
            "file": run.get("file", ""),
            "returncode": run.get("returncode"),
            "timed_out": bool(run.get("timed_out")),
            "printed": bool(run.get("stdout_head")),
        }
        for run in runs
    ]


def _cleanup_sandbox(sandbox: BaseSandbox) -> None:
    """Release a sandbox's images, volumes, containers or venv; a failure is only logged."""
    try:
        sandbox.cleanup()
    except Exception:
        logger.warning("Sandbox cleanup failed", exc_info=True)


def _note_truncation(
    state: AgentState, store: _EventSink | None, role: str, error: Exception
) -> None:
    """Emit ``<role>_output_truncated`` when *error* is model output cut off at its token limit."""
    if isinstance(error, TruncatedOutputError):
        _emit_event(
            state,
            store,
            {
                "event": f"{role}_output_truncated",
                "iteration": state["iteration"],
                "model": error.model,
                "output_tokens": error.output_tokens,
                "max_tokens": error.max_tokens,
            },
        )


def _same_dir(a: Path, b: Path) -> bool:
    """Whether *a* and *b* are the same directory (False when either cannot be read)."""
    try:
        return a.samefile(b)
    except OSError:
        return False


def _url_host(url: str) -> str:
    """The host of *url* ("" when it names none or is malformed)."""
    try:
        return urlparse(url.strip()).hostname or ""
    except ValueError:
        return ""


def _scoped_findings(result: SecurityResult, scope: Collection[str]) -> SecurityResult:
    """*result* without the findings in files outside *scope* (findings naming no file stay)."""

    def keep(entry: Mapping[str, Any]) -> bool:
        file = str(entry.get("file") or "")
        return not file or (normalize_rel_path(file) or file) in scope

    return SecurityResult(
        constraint_violations=[v for v in result.constraint_violations if keep(v)],
        secret_findings=[s for s in result.secret_findings if keep(s)],
        dependency_findings=[d for d in result.dependency_findings if keep(d)],
    )


def _finding_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Security findings counted by kind (``source``) and by severity."""
    items = list(findings)
    return {
        "by_kind": dict(Counter(str(f.get("source", "")) for f in items)),
        "by_severity": dict(Counter(str(f.get("severity", "")) for f in items)),
    }


@dataclass
class Pipeline:
    """The Delegatron orchestration pipeline.

    Manages the full lifecycle: plan -> code -> test -> review -> iterate.
    Uses a simple loop rather than LangGraph's compiled graph to keep
    dependencies minimal for Phase 1. The graph structure is preserved
    in the routing logic for future migration to LangGraph's StateGraph.
    """

    config: Config
    workspace_root: Path
    run_id: str
    artifacts_dir: Path | None = None
    # The branch the run branch forks from; "" = the checked-out branch (the
    # detected default branch when HEAD is detached).
    base_branch: str = ""
    approval_callback: Callable[[dict[str, Any]], Any] | None = field(default=None)
    step_callback: Callable[[str, dict[str, Any]], None] | None = field(default=None)
    # The step callback writes every step to events.jsonl itself (the server's
    # does), so task events are not appended a second time.
    step_callback_persists: bool = False
    # Injectable for tests; defaults to the real `gh` CLI backed creator.
    pr_creator: BasePRCreator | None = field(default=None)
    # Start a run even when the git workspace has uncommitted changes.
    allow_dirty: bool = False
    # Egress of the current run, recorded as it happens (zero-cloud report).
    _net_guard: NetworkGuard = field(default_factory=NetworkGuard, init=False, repr=False)
    # Advisory stuck-pattern detection and call metrics of the current run.
    _observer: ConversationObserver = field(default_factory=create_observer, init=False, repr=False)
    # The current run's wall-time budget (unlimited outside ``execute``).
    _clock: _WallClock = field(default_factory=_WallClock, init=False, repr=False)
    # What each file held before the run first changed it; the security checks
    # judge everything the run has changed since.
    _security_baseline: SecurityBaseline | None = field(default=None, init=False, repr=False)
    # The current run's project constraints (``pipeline.constraints``).
    _run_constraints: Constraints | None = field(default=None, init=False, repr=False)
    # The limit that ended the current session ("" = none; see ``_terminate``).
    _stop_limit: str = field(default="", init=False, repr=False)

    async def execute(
        self,
        request: str,
        initial_state: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> AgentState:
        """Run the full pipeline for a user request.

        Args:
            request: The user's natural-language task description.
            initial_state: Optional saved state from a previous run.  When
                provided the pipeline resumes from the saved iteration and
                skips the architect phase if a work_plan already exists.
            stream: When ``True``, use streaming agent calls and emit
                token-level ``step_callback`` events so the CLI can
                display real-time LLM output.

        Returns the final state with all artifacts and audit trail.
        """
        self._stream = stream
        self._stop_limit = ""
        # The wall-time budget runs from here, less what earlier sessions of
        # this round already used (D13).
        self._clock = _WallClock.start(
            initial_state or {}, self.config.pipeline.max_wall_time_minutes * 60.0
        )

        # Initialize artifact store
        artifacts_base = self.artifacts_dir or (self.workspace_root / "runs")
        store = ArtifactStore(base_dir=artifacts_base, run_id=self.run_id)

        # Set up provider call log; a resumed run starts from its earlier spend.
        call_log = store.run_dir / "provider_calls.jsonl"
        cost_tracker = CostTracker(prior_cost_usd=_prior_cost_usd(initial_state, call_log))
        cost_tracker.set_log_file(call_log)
        store.append_event({"event": "pipeline_start", "request": request, "run_id": self.run_id})
        max_iter = self.config.pipeline.max_iterations

        def _emit_progress(phase: str, iteration: int = 0) -> None:
            """Emit a progress event with current cost/token stats and ETA."""
            # Progress: architect=10%, then each iteration is 90%/max_iter
            if phase == "architect":
                pct = 5
            elif phase == "architect_done":
                pct = 10
            else:
                iter_pct = 90.0 / max(max_iter, 1)
                step_map = {"implementer": 0.0, "sandbox": 0.33, "reviewer": 0.66, "done": 1.0}
                step_frac = step_map.get(phase, 0)
                pct = min(10 + int((iteration - 1 + step_frac) * iter_pct), 99)

            elapsed = self._clock.session_seconds()
            summary = cost_tracker.get_summary()
            state["messages"].append(
                {
                    "event": "progress",
                    "phase": phase,
                    "iteration": iteration,
                    "max_iterations": max_iter,
                    "percent": pct,
                    "elapsed_seconds": round(elapsed, 1),
                    "total_tokens": summary["total_input_tokens"] + summary["total_output_tokens"],
                    "total_cost_usd": round(summary["total_cost_usd"], 6),
                    "estimated_remaining_seconds": (
                        round(elapsed / max(pct, 1) * (100 - pct), 0) if pct > 5 else None
                    ),
                }
            )

        workspace = Workspace(
            repo_path=self.workspace_root,
            run_id=self.run_id,
            base_branch=self.base_branch,
            run_git_hooks=self.config.pipeline.run_git_hooks,
        )
        # A subdirectory of a repository is a git workspace too, unless that
        # repository ignores it (the run would switch its branch for nothing).
        repo_root = Workspace.find_repo_root(self.workspace_root)
        ignored_by_repo = repo_root is not None and Workspace.ignored_by_repo(self.workspace_root)
        is_git_repo = repo_root is not None and not ignored_by_repo

        state = _new_state(request, self.workspace_root)
        if initial_state is not None:
            _restore_state(state, initial_state)
            state["total_cost_usd"] = cost_tracker.cumulative_cost_usd
            logger.info("Resumed from saved state at iteration %d", state["iteration"])

        # Initialize outside try so finalization block always has valid refs
        self._observer = create_observer()
        net_guard = self._net_guard = NetworkGuard.for_config(self.run_id, self.config)
        self._security_baseline = None
        self._run_constraints = None
        sandbox: BaseSandbox | None = None

        try:
            if ignored_by_repo:
                logger.warning(
                    "The git repository at %s ignores the workspace; running without git",
                    repo_root,
                )
                _emit_event(
                    state,
                    store,
                    {
                        "event": "workspace_not_git",
                        "reason": "ignored",
                        "repo_root": str(repo_root),
                    },
                )
            # Fail closed: without the run branch, commits would land on the
            # user's current branch.
            if is_git_repo and not self._init_workspace(
                state, workspace, store, resumed=initial_state is not None, repo_root=repo_root
            ):
                raise _EarlyExit
            self._security_baseline = self._new_security_baseline(state, workspace)
            parallel = self._parallel_mode(state, store, workspace)

            # Detect after the run branch is checked out (a resumed run's branch
            # holds the files of its earlier iterations).
            project_info = detect_project(self.workspace_root)
            state["project_info"] = _project_info_dict(project_info)
            state["file_tree"] = workspace.get_file_tree()
            if initial_state is not None:
                self._resume_after_restore(state, store)

            # Create agents (the director is created when a conflict needs it)
            architect = self._create_architect()
            implementer = self._create_implementer()
            reviewer = self._create_reviewer()
            logger.info("Roles: %s", self._effective_roles())

            # Create sandbox based on config (re-created when a patch changes the
            # detected toolchain).  LLM and sandbox egress is recorded per call
            # (``_call_agent`` / ``_run_sandbox``).
            sandbox_spec = self._sandbox_spec(project_info)
            sandbox = create_sandbox(**sandbox_spec)
            net_guard.record_artifact_store(
                store_type="local_fs",
                path=str(store.run_dir),
            )

            # Load project constraints
            self._run_constraints = self._project_constraints()
            # Step 1: Architect creates the plan (skip if resuming with existing plan)
            if initial_state is not None and state.get("plan") is not None:
                logger.info("Resuming with existing plan — skipping architect phase")
                store.append_event({"event": "resume_skip_architect"})
                if state.get("_plan_pending_approval"):
                    # The run stopped while its plan awaited approval: ask again.
                    await self._approve_first_plan(state, store)
            else:
                _emit_progress("architect")
                mark = _StepMark.take(cost_tracker, state)
                state = await self._run_architect(state, architect, cost_tracker, store)
                self._observe_step("architect", state, cost_tracker, mark, state.get("plan"))
                _emit_progress("architect_done")
                if state["status"] == "error":
                    store.append_event({"event": "architect_error", "error": state["error"]})
                    raise _EarlyExit

                # Save plan artifact
                plan_snapshot = state.get("plan")
                if plan_snapshot:
                    store.save_plan(plan_snapshot)
                    store.append_event(
                        {"event": "plan_saved", "tasks": len(plan_snapshot.get("tasks", []))}
                    )
                await self._approve_first_plan(state, store)

            # Step 1b: what the workspace's tests already fail, before any patch
            await self._take_baseline(state, sandbox, store)

            # Main loop: implement -> test -> review -> route
            while state["status"] == "running":
                # A re-plan the last review asked for comes first (unless a limit
                # already stops the run): it restarts the task it replaces.
                if state.get("_replan") and self._stop_reason(state) is None:
                    _emit_progress("architect")
                    state = await self._replan(state, architect, cost_tracker, store, workspace)
                    _emit_progress("architect_done")
                    if state["status"] == "error":
                        break

                # Per-task iteration limit: in a multi-task plan an exhausted
                # task is skipped instead of ending the whole run.
                if self._task_exhausted(state):
                    logger.warning(
                        "Task %s exhausted %d iterations",
                        (state.get("current_task") or {}).get("task_id", ""),
                        self.config.pipeline.max_iterations,
                    )
                    if self._finish_task(state, store, "exhausted"):
                        self._checkpoint(state, store)
                        continue
                    self._terminate(
                        state,
                        store,
                        "Last pending task exhausted its per-task iteration limit",
                        limit="task_iterations",
                    )
                    break

                # Global limits (cost, the iteration cap, wall time), checked
                # before the iteration is counted: a stop never counts one that
                # did not run.  A pending re-plan is never started past a limit.
                stop = self._stop_reason(state)
                if stop is not None:
                    phase = "replan" if state.get("_replan") else "iteration_start"
                    self._terminate(state, store, stop[1], limit=stop[0], phase=phase)
                    break

                state["iteration"] += 1
                logger.info("=== Iteration %d ===", state["iteration"])

                # Step 2: Implementer generates code; in parallel mode the first
                # patches of the other ready tasks are generated alongside.
                _emit_progress("implementer", state["iteration"])
                mark = _StepMark.take(cost_tracker, state)
                siblings = self._parallel_siblings(state) if parallel else []
                if siblings:
                    state = await self._run_parallel_implementers(
                        state, implementer, cost_tracker, store, siblings
                    )
                else:
                    state = await self._run_implementer(state, implementer, cost_tracker, store)
                self._observe_step(
                    "implementer", state, cost_tracker, mark, state.get("current_patch")
                )
                if state["status"] == "error":
                    break

                # Without a patch (the implementer call failed) the attempt ends
                # with the synthesized review already in place; so does one whose
                # patch is turned back unapplied (``_apply_or_turn_back``).
                mark = _StepMark.take(cost_tracker, state)
                if state.get("current_patch") is not None:
                    self._record_patch(state, store)

                    # Steps 2b-2e: security checks, then apply (and commit in git)
                    if self._apply_or_turn_back(state, store, workspace, is_git_repo=is_git_repo):
                        # Step 2f: Import validation against the post-apply tree
                        self._run_import_validation(state, store)

                        # Step 3: Sandbox runs tests (for the toolchain now detected)
                        sandbox_spec, sandbox = self._reconfigure_sandbox(
                            state, store, sandbox_spec, sandbox
                        )
                        _emit_progress("sandbox", state["iteration"])
                        state = await self._run_sandbox(state, sandbox, store)
                        self._check_test_suite(state, store)
                        self._record_test_outcomes(state, store)
                        self._record_test_report(state, store)

                        # Step 4: Reviewer evaluates — an attempt that changed
                        # nothing and still fails keeps the previous review instead
                        _emit_progress("reviewer", state["iteration"])
                        report = state.get("test_report")
                        if state.get("_noop_streak") and classify_test_evidence(report) == "failed":
                            self._carry_review_forward(state)
                        else:
                            state = await self._run_reviewer(state, reviewer, cost_tracker, store)
                else:
                    # No patch (the implementer call failed): nothing applied, no findings.
                    self._run_import_validation(state, store)
                # A synthesized review (no reviewer call) is recorded too: the
                # observer judges the loop's rhythm per step.
                self._observe_step(
                    "reviewer", state, cost_tracker, mark, state.get("review_report")
                )
                if state["status"] == "error":
                    break

                # Step 4b: Approval (hard gates, orchestrator evidence, EMCS);
                # 4c: arbitration of a conflict that recurs on this task.  Both
                # are settled before the review is persisted, and removed tests
                # veto any approval either gave.
                emcs_override = self._judge_review(state, store)
                await self._maybe_arbitrate(state, cost_tracker, store)
                _block_regressions(state)
                self._check_stuck(state)
                self._record_review(state, store, emcs_override=emcs_override)

                # Steps 5-6: complete the task (and move on) or route the next pass
                self._conclude_pass(state, store)

        except _EarlyExit:
            pass
        except WallTimeExceeded as e:
            # The deadline cancelled an agent call or a sandbox run: the run is
            # out of budget (not failed), and finalizes like any other stop.
            self._terminate(
                state, store, self._wall_time_reason(), limit="wall_time", phase=e.phase
            )
        except Exception as e:
            error_type = type(e).__name__
            error_msg = f"{error_type}: {e}"
            logger.exception("Pipeline error (%s)", error_type)
            state["status"] = "error"
            state["is_approved"] = False
            state["error"] = error_msg
            state["messages"].append({"event": "error", "error": error_msg, "type": error_type})
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._checkpoint(state, store)
            raise
        finally:
            # Free GPU memory by unloading Ollama models and remove what the
            # sandbox created — on every exit path, including cancellation.
            cleanup_after_run(self.config)
            if sandbox is not None:
                _cleanup_sandbox(sandbox)

        # Every task gets an outcome; a run that delivered only part of the work
        # (some tasks, or with an approved task's test broken) ends "partial".
        _close_task_outcomes(state)
        regressed = _regressed_tests(state)
        _mark_regressions(state, regressed)
        _settle_run_status(state, budget_stop=self._stop_limit in _BUDGET_LIMITS)
        # The run branch holds every path written, also those a resumed run
        # wrote before it stopped (a failure here keeps a PR from being opened).
        if is_git_repo and workspace.branch:
            message = f"{RUN_COMMIT_PREFIX} {state['iteration']} — uncommitted changes"
            self._commit(state, workspace, message, store)
        # The last checkpoint says how the run ended (resume and refine read it).
        self._save_final_checkpoint(state, store)

        # Final summary (elapsed time and cost cover every session of the run)
        elapsed = self._clock.elapsed()
        history = state.get("_file_history") or {}
        summary = {
            "run_id": self.run_id,
            "status": state["status"],
            "iterations": state["iteration"],
            "total_cost_usd": cost_tracker.cumulative_cost_usd,
            "prior_cost_usd": cost_tracker.prior_cost_usd,
            "session_cost_usd": cost_tracker.total_cost_usd,
            "elapsed_seconds": round(elapsed, 1),
            "prior_elapsed_seconds": round(self._clock.prior_seconds, 1),
            "session_elapsed_seconds": round(self._clock.session_seconds(), 1),
            "request": request,
            "plan_summary": (state.get("plan") or {}).get("summary", ""),
            "is_approved": state.get("is_approved", False),
            "error": state.get("error", ""),
            "termination_reason": state.get("_termination_reason", ""),
            "workspace_root": str(self.workspace_root),
            "branch": workspace.branch,
            # files the run left changed; those it deleted are listed apart
            "files_changed": sorted(p for p, entry in history.items() if not entry.get("deleted")),
            "files_deleted": sorted(p for p, entry in history.items() if entry.get("deleted")),
            "task_outcomes": task_outcome_rows(state),
            # every task the run did not deliver (given up, stopped or never reached)
            "skipped_tasks": [
                row["task_id"] for row in task_outcome_rows(state) if row["status"] != "approved"
            ],
            "baseline_failures": state.get("_known_failures") or {},
            "fixed_baseline_failures": state.get("_fixed_failures") or {},
            "regressed_tests": regressed,
            # every nondeterministic test the run met, also those made deterministic since
            "flaky_tests": sorted(
                {
                    **(state.get("_known_flaky_tests") or {}),
                    **(state.get("_flaky_seen") or {}),
                }.values(),
                key=lambda r: r["test"],
            ),
            "security": self._security_summary(state),
            "roles": self._effective_roles(),
            "cost_breakdown": cost_tracker.get_summary(),
            "observer_metrics": self._observer.get_metrics(),
        }

        # Optional: open a GitHub PR when the run was approved.
        # Never fails the run — errors are recorded and logged.
        pr_result = self._maybe_create_pr(state, workspace, summary, is_git_repo)
        if pr_result.get("created") or pr_result.get("error") or pr_result.get("skipped_reason"):
            summary["pull_request"] = pr_result

        # Zero-cloud verification, after the last possible egress (the PR push;
        # the server's failure webhook goes out after the report is written)
        self._record_alert_egress(state)
        summary["zero_cloud"] = self._finalize_zero_cloud(net_guard, store)
        try:
            store.save_summary(summary)
        except Exception as e:
            logger.critical("Failed to save summary for run %s: %s", self.run_id, e)
            try:
                (store.run_dir / "summary.json").write_text(
                    json.dumps(summary, indent=2, default=str), encoding="utf-8"
                )
            except Exception:
                logger.critical("Fallback summary write also failed for run %s", self.run_id)
        store.append_event({"event": "pipeline_complete", **summary})

        state["messages"].append(
            {
                "event": "pipeline_complete",
                "status": state["status"],
                "iterations": state["iteration"],
                "total_cost_usd": state["total_cost_usd"],
                "elapsed_seconds": round(elapsed, 1),
            }
        )

        return state

    def _notify_step(self, step: str, data: dict[str, Any]) -> None:
        """Invoke the step_callback if one has been provided."""
        if self.step_callback is not None:
            try:
                self.step_callback(step, data)
            except Exception:
                logger.debug("step_callback raised for step=%s", step, exc_info=True)

    def _checkpoint(self, state: AgentState, store: ArtifactStore) -> None:
        """Save the state, with the wall time the run has used so far (``_wall_clock``)."""
        state["_wall_clock"] = self._clock.checkpoint()
        store.save_state(dict(state))

    async def _within_deadline(self, phase: str, call: Callable[[], Awaitable[_T]]) -> _T:
        """Run *call* within the run's remaining wall time (D13).

        Raises ``WallTimeExceeded`` before *call* starts when no time is left,
        and when the deadline cancels it.  A ``TimeoutError`` of the call's
        own (a provider timeout) is re-raised as it is.
        """
        remaining = self._clock.remaining()
        if remaining <= 0:
            raise WallTimeExceeded(phase)
        if math.isinf(remaining):
            return await call()
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await call()
        except TimeoutError:
            if deadline.expired():
                logger.warning("Wall time ran out during %s; cancelled", phase)
                raise WallTimeExceeded(phase) from None
            raise

    def _wall_time_reason(self) -> str:
        """The termination reason of a run whose wall-time budget ran out."""
        return f"Wall time limit ({self.config.pipeline.max_wall_time_minutes}min) reached"

    def _project_constraints(self) -> Constraints | None:
        """The project constraints the security checks enforce (``pipeline.constraints``)."""
        data = self.config.pipeline.constraints
        return Constraints.from_dict(data) if data else None

    def _new_security_baseline(self, state: AgentState, workspace: Workspace) -> SecurityBaseline:
        """The run's ``SecurityBaseline``: what each file held before the run changed it.

        A resumed run's earlier changes (``_file_history``) are read from the
        run branch's fork point, when there is one.
        """
        changed = list(state.get("_file_history") or {})
        return SecurityBaseline(
            self.workspace_root,
            changed_paths=changed,
            git_ref=workspace.initial_sha if changed and workspace.branch else "",
        )

    def _security_summary(self, state: AgentState) -> dict[str, Any]:
        """The summary's ``security`` section.

        ``unresolved_findings`` are the findings in everything the run left
        changed (a scan against the run's baseline, or the last pass's findings
        when none can be made; empty in "allow" mode), counted by kind and
        severity (``unresolved``); ``unresolved_blocking`` counts those that
        block in "block" mode.  ``detected`` counts the distinct findings the
        run met on the way (``_security_detected``), and ``blocked_patches``
        the patches "block" mode kept out of the workspace.
        """
        mode = self.config.pipeline.security_mode
        unresolved: list[dict[str, Any]] = []
        blocking: list[dict[str, Any]] = []
        if mode != "allow":
            result = self._final_security_scan()
            if result is None:
                unresolved = list(state.get("security_findings") or [])
                blocking = list(state.get("_security_blockers") or [])
            else:
                unresolved, blocking = result.findings, blocking_findings(result, mode)
        return {
            "mode": mode,
            "unresolved_findings": unresolved,
            "unresolved": _finding_counts(unresolved),
            "unresolved_blocking": len(blocking),
            "detected": _finding_counts((state.get("_security_detected") or {}).values()),
            "blocked_patches": sum(
                1 for m in state["messages"] if m.get("event") == "security_blocked"
            ),
        }

    def _final_security_scan(self) -> SecurityResult | None:
        """The security checks on everything the run left changed (None when not possible)."""
        if self._security_baseline is None:
            return None
        try:
            return run_security_checks(
                {"files": {}},
                self._run_constraints,
                self.workspace_root,
                secrets_allowlist=self.config.security.secrets_allowlist,
                baseline=self._security_baseline,
            )
        except Exception as e:
            logger.warning("Final security scan failed: %s", e)
            return None

    def _record_alert_egress(self, state: AgentState) -> None:
        """Record the failure webhook the server sends for this run's status, as egress."""
        url = os.environ.get(ALERT_WEBHOOK_ENV, "")
        if url.strip() and state["status"] in ALERT_STATUSES:
            self._net_guard.record_external_call("notification", _url_host(url))

    async def _approve_first_plan(self, state: AgentState, store: ArtifactStore) -> None:
        """Announce the run's plan, have it approved in interactive mode, then checkpoint it.

        Until it is approved the plan is marked ``_plan_pending_approval``, so a
        run stopped meanwhile (cancelled while waiting) asks again on resume
        instead of implementing a plan nobody approved.  A rejection ends the
        run (``plan_rejected``).
        """
        state["_plan_pending_approval"] = True
        if not await self._announce_plan(state, state.get("plan") or {}):
            state["status"] = "error"
            state["error"] = "Plan rejected by user"
            _emit_event(state, store, {"event": "plan_rejected"})
            raise _EarlyExit
        state["_plan_pending_approval"] = False
        # Checkpoint the plan so a resume does not re-run the architect.
        self._checkpoint(state, store)

    async def _take_baseline(
        self, state: AgentState, sandbox: BaseSandbox, store: ArtifactStore
    ) -> None:
        """The pre-run baseline (``baseline_tests``): record the tests that already fail.

        Before the run's first patch, when the workspace has tests the sandbox
        can run, it runs them once without a patch.  Each failing test becomes
        a known failure of origin ``PRE_EXISTING``: it never blocks a task and
        reaches the agents as pre-existing.  The report is saved as
        ``BASELINE_REPORT_ARTIFACT`` and reused by a resume that finds it;
        ``_baseline_done`` keeps a resume or refine from taking another, and a
        run that already applied a patch never takes one (its failures could
        be the run's own).  Failures that name no test record nothing.
        """
        if state.get("_baseline_done"):
            return
        state["_baseline_done"] = True
        if (
            not self.config.pipeline.baseline_tests
            or state["iteration"]
            or state.get("_file_history")
        ):
            return
        report = _load_json_artifact(store, BASELINE_REPORT_ARTIFACT)
        if report is None:
            has_tests = any(map(is_test_path, state.get("file_tree") or []))
            testable = bool(self.config.pipeline.docker_image) or self._can_sandbox_test(state)
            if not (has_tests and testable):
                return
            report = await self._run_baseline(state, sandbox, store)
            if report is None:
                return
        ids = failing_test_ids(report)
        known = state.setdefault("_known_failures", {})
        for nodeid in sorted(ids or ()):
            known.setdefault(nodeid, PRE_EXISTING)
        if ids is None and classify_test_evidence(report) == "failed":
            logger.warning("The pre-run test failures name no test; none are pre-existing")
        _emit_event(
            state,
            store,
            {
                "event": "baseline_tests",
                "passed": bool(report.get("passed")),
                "total": report.get("total", 0),
                "pre_existing": sorted(ids) if ids is not None else None,
            },
        )
        self._checkpoint(state, store)

    async def _run_baseline(
        self, state: AgentState, sandbox: BaseSandbox, store: ArtifactStore
    ) -> dict[str, Any] | None:
        """Run the workspace's tests without a patch and save the report (None when it failed).

        A run the wall-time deadline cuts short leaves the baseline to be taken
        by a resume (``_baseline_done`` is reset) and ends the run.
        """
        logger.info("Running the pre-run test baseline...")
        empty_patch = {"files": {}, "delete_files": [], "diff_unified": "", "rationale": ""}
        pipeline = self.config.pipeline
        try:
            self._net_guard.record_sandbox_call(sandbox_type=pipeline.sandbox)
            report = await self._within_deadline(
                "baseline",
                lambda: sandbox.apply_and_test({**empty_patch, **self._flaky_options(state)}),
            )
        except WallTimeExceeded:
            state["_baseline_done"] = False  # cut short: a resume takes it
            raise
        except Exception as e:
            logger.warning("The pre-run test baseline failed: %s", e)
            _emit_event(state, store, {"event": "baseline_tests_failed", "error": str(e)})
            return None
        report.pop("_sandbox", None)
        _save_json_artifact(store, BASELINE_REPORT_ARTIFACT, report)
        return report

    def _task_exhausted(self, state: AgentState) -> bool:
        """Whether the current task of a multi-task plan used up its attempts unapproved."""
        return (
            len(state.get("all_tasks") or []) > 1
            and state.get("_task_iteration", 0) >= self.config.pipeline.max_iterations
            and not state.get("is_approved")
        )

    def _stop_reason(self, state: AgentState) -> tuple[str, str] | None:
        """Which limit stops the run before another iteration, and why: ``(limit, reason)``.

        *limit* is "iterations" (the run's iteration cap), "cost"
        (``max_cost_usd``; 0 = none) or "wall_time"; the reason names the
        limit that fired.
        """
        pipeline = self.config.pipeline
        cap = _iteration_cap(state, pipeline.max_iterations)
        if state["iteration"] >= cap:
            return "iterations", f"Iteration limit ({cap}) reached"
        cost = float(state.get("total_cost_usd") or 0.0)
        if pipeline.max_cost_usd > 0 and cost >= pipeline.max_cost_usd:
            return "cost", f"Cost limit (${pipeline.max_cost_usd:.2f}) reached: ${cost:.2f} spent"
        if self._clock.remaining() <= 0:
            return "wall_time", self._wall_time_reason()
        return None

    def _terminate(
        self,
        state: AgentState,
        store: ArtifactStore,
        reason: str,
        *,
        limit: str,
        phase: str = "iteration_start",
    ) -> None:
        """End the run "exhausted" on a limit; the reason reaches the events and the summary.

        ``terminated`` names the *limit*, the *phase* the run was in, the
        iteration and the wall time the run has taken.
        """
        logger.warning("Run stopped: %s (%s)", reason, phase)
        self._stop_limit = limit
        state["status"] = "exhausted"
        state["is_approved"] = False
        state["_termination_reason"] = reason
        _emit_event(
            state,
            store,
            {
                "event": "terminated",
                "reason": reason,
                "limit": limit,
                "phase": phase,
                "iteration": state["iteration"],
                "elapsed_seconds": round(self._clock.elapsed(), 1),
            },
        )

    def _check_stuck(self, state: AgentState) -> None:
        """Ask the observer for a stuck pattern (advisory ``stuck_detected``)."""
        stuck = self._observer.check_stuck(noop_streak=state.get("_noop_streak", 0))
        if stuck:
            logger.warning(
                "Stuck pattern detected: %s (iteration %d)", stuck.value, state["iteration"]
            )
            state["messages"].append(
                {"event": "stuck_detected", "pattern": stuck.value, "iteration": state["iteration"]}
            )

    def _record_review(
        self, state: AgentState, store: ArtifactStore, *, emcs_override: bool
    ) -> None:
        """Persist the settled review of this pass and announce the verdict.

        Saved after approval and arbitration, so ``review_report_00N.json``,
        ``review_saved``, ``reviewer_reasoning`` and ``reviewer_complete`` all
        show the verdict the pass ends with.
        """
        review = state.get("review_report") or {}
        accepted = state.get("is_approved", False)
        if review:
            store.save_review(state["iteration"], review)
            store.append_event(
                {
                    "event": "review_saved",
                    "iteration": state["iteration"],
                    "reviewer_accepted": review.get("acceptance_met", False),
                    "accepted": accepted,
                    "emcs_override": emcs_override,
                    "synthesized": bool(review.get("synthesized")),
                }
            )
            # Emit reviewer reasoning for live dashboard display
            issues = review.get("issues", [])
            state["messages"].append(
                {
                    "event": "reviewer_reasoning",
                    "iteration": state["iteration"],
                    "summary": review.get("summary", ""),
                    "reviewer_accepted": review.get("acceptance_met", False),
                    "accepted": accepted,
                    "emcs_override": emcs_override,
                    "issues": [
                        {
                            "severity": i.get("severity", "info"),
                            "title": i.get("title", ""),
                            "details": i.get("details", ""),
                            "recommended_fix": i.get("recommended_fix", ""),
                        }
                        for i in (issues if isinstance(issues, list) else [])
                    ],
                }
            )
        self._notify_step(
            "reviewer_complete",
            {
                "iteration": state["iteration"],
                "reviewer_accepted": review.get("acceptance_met", False),
                "accepted": accepted,
                "emcs_override": emcs_override,
            },
        )

    def _save_final_checkpoint(self, state: AgentState, store: ArtifactStore) -> None:
        """Checkpoint the run as it ended, on every exit path that finalizes the run.

        Overwrites the last pass's checkpoint, so it never says "running" for a
        run that stopped on a limit or an error, and it carries the wall time
        the run has taken (a resume starts from it).  Never raises.
        """
        try:
            self._checkpoint(state, store)
        except Exception as e:
            logger.error("Could not save the final checkpoint of run %s: %s", self.run_id, e)

    def _finalize_zero_cloud(self, net_guard: NetworkGuard, store: ArtifactStore) -> dict[str, Any]:
        """Close the run's egress record: the summary entry, plus ``zero_cloud_report.json``.

        Never raises: a failure is logged and reported as unverified.
        """
        try:
            report = net_guard.finalize()
        except Exception as e:
            logger.error("Zero-cloud verification failed for run %s: %s", self.run_id, e)
            return {"verified": False, "violations": 0, "fingerprint": "", "error": str(e)}
        try:
            # The full report, as a separate artifact for compliance export
            (store.run_dir / "zero_cloud_report.json").write_text(
                json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as e:
            logger.error("Could not save the zero-cloud report of run %s: %s", self.run_id, e)
        return {
            "verified": report.cloud_free,
            "violations": len(report.violations),
            "fingerprint": report.fingerprint,
        }

    def _init_workspace(
        self,
        state: AgentState,
        workspace: Workspace,
        store: ArtifactStore,
        *,
        resumed: bool,
        repo_root: Path | None = None,
    ) -> bool:
        """Check out the run branch; on failure set ``status="error"`` and return False.

        Only a *resumed* run (resume, refine) continues on its existing run
        branch; a fresh run never starts from an earlier run's code (D8).  A
        workspace inside a larger repository (*repo_root*) gets that branch in
        the enclosing repository; a dirty-tree error then says so.
        """
        try:
            info = workspace.initialize(allow_dirty=self.allow_dirty, fresh=not resumed)
        except WorkspaceInitError as e:
            error = str(e)
            nested = repo_root is not None and not _same_dir(repo_root, self.workspace_root)
            if isinstance(e, DirtyWorkspaceError) and nested:
                error += f" (the workspace is inside the git repository at {repo_root})"
            logger.error("Workspace setup failed: %s", error)
            state["status"] = "error"
            state["error"] = f"Workspace setup failed: {error}"
            _emit_event(state, store, {"event": "workspace_init_failed", "error": error})
            return False
        if resumed:
            # Files written before the resume may not have been committed yet.
            workspace.mark_pending(state.get("_file_history") or {})
        state["workspace_sha"] = info.commit_sha
        logger.info("Workspace initialized: branch=%s, commit=%s", info.branch, info.commit_sha[:8])
        _emit_event(
            state,
            store,
            {
                "event": "workspace_initialized",
                "branch": info.branch,
                "base_branch": info.base_branch,
                "original_branch": workspace.original_branch,
                "initial_sha": workspace.initial_sha,
                "resumed": workspace.resumed,
            },
        )
        return True

    def _resume_after_restore(self, state: AgentState, store: ArtifactStore) -> None:
        """Act on a restored pass/approval instead of letting it hit the budget check.

        A task that passed the completion gate advances to the next pending task;
        an approval with nothing left to do finishes the run as approved (never
        with accepted tests missing).  A current task that had been given up
        (stalled, exhausted) is re-opened for this session (``restart_current_task``);
        one already approved is never run again (``_resume_past_approved``).
        Outcomes the last session closed for tasks it never finished are
        dropped: those tasks are still to do.
        """
        finished = _finished_task_ids(state)
        outcomes = state.get("_task_outcomes") or {}
        for task_id in [task_id for task_id in outcomes if task_id not in finished]:
            del outcomes[task_id]
        _block_regressions(state)
        if _task_passed(state) and self._finish_task(state, store, "approved"):
            return
        if state.get("is_approved"):
            state["status"] = "approved"
            return
        current_id = (state.get("current_task") or {}).get("task_id")
        if current_id in (state.get("_skipped_tasks") or []):
            logger.info("Resuming task %s, which had been given up", current_id)
            restart_current_task(state)
        elif current_id in (state.get("_completed_tasks") or []):
            self._resume_past_approved(state, store)

    def _resume_past_approved(self, state: AgentState, store: ArtifactStore) -> None:
        """Resume a run whose current task is already approved (a "partial" run's checkpoint).

        The next pending task becomes current.  With none left, the tasks given
        up are re-opened (``_reopen_task``; ``tasks_reopened``) and the first
        ready one becomes current.  With every task approved the run is
        approved ("partial" while an accepted test is missing).
        """
        tasks = state.get("all_tasks") or []
        next_task = _next_pending_task(tasks, _finished_task_ids(state))
        if next_task is None:
            plan_ids = {task.get("task_id") for task in tasks}
            reopened = [i for i in state.get("_skipped_tasks") or [] if i in plan_ids]
            if not reopened:
                state["is_approved"] = not state.get("_removed_tests")
                state["status"] = "approved" if state["is_approved"] else "partial"
                return
            for task_id in reopened:
                _reopen_task(state, task_id)
            logger.info("Resuming the tasks that had been given up: %s", reopened)
            _emit_event(
                state,
                store,
                {"event": "tasks_reopened", "iteration": state["iteration"], "tasks": reopened},
            )
            next_task = _next_pending_task(tasks, _finished_task_ids(state)) or tasks[0]
        _reset_for_next_task(state, next_task)
        self._observer.reset()

    def _conclude_pass(self, state: AgentState, store: ArtifactStore) -> None:
        """End an implement/test/review pass, then checkpoint.

        A task that passes the completion gate is finished and the next pending
        task becomes current; the last task falls through to routing, which
        approves the run (removed tests veto that approval).  Otherwise the
        stall policy is applied (a stalled task is skipped; the run ends
        "stalled" when no task is left) or routing picks the next step.  The
        checkpoint records the routed state, so a resume continues from here.
        """
        _block_regressions(state)
        state["_task_iteration"] = state.get("_task_iteration", 0) + 1
        if not (_task_passed(state) and self._finish_task(state, store, "approved")):
            stall = self._detect_stall(state, store)
            if stall is None:
                self._route(state, store)
            elif not self._finish_task(state, store, "stalled", reason=stall):
                state["status"] = "stalled"
        self._checkpoint(state, store)

    def _detect_stall(self, state: AgentState, store: ArtifactStore) -> str | None:
        """Apply the stall policy (``should_stop_for_stall``) to the attempt just concluded.

        The grace period counts attempts on the current task, more for a local
        implementer.  A stall emits ``stall_detected`` with its reason.
        """
        local = is_local_base_url(self.config.implementer.base_url)
        min_attempts = LOCAL_STALL_ATTEMPTS if local else CLOUD_STALL_ATTEMPTS
        reason = should_stop_for_stall(state, min_attempts=min_attempts)
        if reason is not None:
            task_id = (state.get("current_task") or {}).get("task_id", "")
            attempt = state.get("_task_iteration", 0)
            logger.warning("Stall detected (%s) on attempt %d of task %s", reason, attempt, task_id)
            _emit_event(
                state,
                store,
                {
                    "event": "stall_detected",
                    "iteration": state["iteration"],
                    "task_id": task_id,
                    "task_attempt": attempt,
                    "reason": reason,
                },
            )
        return reason

    def _finish_task(
        self, state: AgentState, store: ArtifactStore, status: str, *, reason: str = ""
    ) -> bool:
        """Record how the current task ended and make the next pending task current.

        *status* is ``"approved"`` (the task joins ``_completed_tasks`` and its
        passing tests become protected) or why it was given up (``"exhausted"``,
        ``"stalled"``: it joins ``_skipped_tasks``, never both, and the tests it
        left failing and the accepted tests it removed stop blocking later
        tasks).  A stall's *reason* is kept in the task's outcome.  Emits one
        task event when another task follows, and the observer starts over for
        it.  Returns False when no task is left.
        """
        task_id = (state.get("current_task") or {}).get("task_id", "")
        outcome: dict[str, Any] = {"status": status, "iterations": state.get("_task_iteration", 0)}
        if reason:
            outcome["reason"] = reason
        state.setdefault("_task_outcomes", {})[task_id] = outcome
        event: dict[str, Any]
        if status == "approved":
            _remove_task_id(state, "_skipped_tasks", task_id)
            _add_task_id(state, "_completed_tasks", task_id)
            self._record_accepted_tests(state)
            event = {"event": "task_advanced", "completed_task": task_id}
        else:
            _remove_task_id(state, "_completed_tasks", task_id)
            _add_task_id(state, "_skipped_tasks", task_id)
            left_failing = _record_known_failures(state, task_id)
            left_removed = _record_known_removals(state, task_id)
            event = {
                "event": "task_skipped",
                "skipped_task": task_id,
                "reason": status,
                "left_failing": left_failing,
            }
            accepted = set(state.get("_accepted_test_ids") or [])
            regressed = sorted({*(i for i in left_failing if i in accepted), *left_removed})
            if regressed:
                # Excused for later tasks, but the run cannot be approved with them.
                logger.warning("Task %s left accepted tests broken: %s", task_id, regressed)
                event["regressed_tests"] = regressed
            if reason:
                event["stall_reason"] = reason

        all_tasks = state.get("all_tasks") or []
        finished = _finished_task_ids(state)
        next_task = _next_pending_task(all_tasks, finished)
        if next_task is None:
            return False
        if next_task.get("task_id") == task_id:
            # Never "advance" onto the task just finished (a selector bug would loop).
            logger.error("Task selection returned the finished task %s; no task follows", task_id)
            return False
        logger.info("Task %s %s — next: %s", task_id, status, next_task.get("task_id"))
        _reset_for_next_task(state, next_task)
        self._observer.reset()
        plan_ids = {task.get("task_id") for task in all_tasks}
        event.update(
            next_task=next_task.get("task_id"),
            next_title=next_task.get("title", ""),
            progress=f"{len(finished & plan_ids)}/{len(all_tasks)}",
        )
        self._emit_step_event(state, store, event)
        return True

    def _emit_step_event(
        self, state: AgentState, store: _EventSink | None, event: dict[str, Any]
    ) -> None:
        """Record an event that is also a step once: in the state, as a step, in ``events.jsonl``.

        A step callback that persists every step already writes it to
        ``events.jsonl``, so the store is skipped then.  The step carries the
        event as the store would write it (tagged with its task for a task
        generated in parallel: ``_SiblingEvents``).
        """
        state["messages"].append(event)
        stored = store.tag(event) if isinstance(store, _SiblingEvents) else event
        self._notify_step(event["event"], {k: v for k, v in stored.items() if k != "event"})
        persisted = self.step_callback_persists and self.step_callback is not None
        if store is not None and not persisted:
            store.append_event(dict(event))

    def _record_accepted_tests(self, state: AgentState) -> None:
        """Protect the tests the approved task left passing (``_accepted_test_ids``).

        Each is owned by the first task that accepted it (``_accepted_test_owners``).
        The ids come from the run's per-test results; for a runner without them,
        from the Python test files the task wrote.  Only tests whose file is in
        the workspace count (not the sandbox-only smoke tests).
        """
        root = Path(state["workspace_root"])
        task_id = (state.get("current_task") or {}).get("task_id", "")
        ids = passing_test_ids(state.get("test_report"))
        if not ids:
            own_tests = [
                path
                for path, entry in (state.get("_file_history") or {}).items()
                if entry.get("task_id") == task_id and path.endswith(".py") and is_test_path(path)
            ]
            ids = [
                f"{path}::{name}"
                for path, source in _read_workspace_files(root, own_tests).items()
                if source is not None
                for name in sorted(extract_test_functions(source))
            ]
        on_disk = {
            file
            for file, source in _read_workspace_files(root, {node_file(i) for i in ids}).items()
            if source is not None
        }
        new_ids = [nodeid for nodeid in ids if node_file(nodeid) in on_disk]
        state["_accepted_test_ids"] = sorted({*(state.get("_accepted_test_ids") or []), *new_ids})
        owners = state.setdefault("_accepted_test_owners", {})
        for nodeid in new_ids:
            owners.setdefault(nodeid, task_id)

    def _check_test_suite(self, state: AgentState, store: ArtifactStore) -> None:
        """Judge the latest run against the suite's history.

        Known failures (left by a skipped task, or failing before the run) are
        split off as ``baseline_failures`` (``task_passed`` says whether only
        those failed, judged against the run's test commands); a known failure
        that now passes moves to ``_fixed_failures``.  Accepted tests of earlier
        tasks that went missing from the workspace or the run become
        ``_removed_tests`` (recomputed every pass, so restoring the tests clears
        it), per file with the task that accepted them (``owner_task``) and where
        they went missing (``source``: "static" from the file on disk, "sandbox"
        only from a complete run).  Those a skipped task removed
        (``_known_removals``) are left out until they are back.
        """
        report = state.get("test_report")
        known = state.get("_known_failures") or {}
        fixed = state.setdefault("_fixed_failures", {})
        for nodeid in failing_test_ids(report) or ():
            fixed.pop(nodeid, None)  # failing again: not fixed after all
        if report and known:
            for nodeid in passing_test_ids(report):
                if nodeid in known:
                    fixed[nodeid] = known.pop(nodeid)
            task_id = (state.get("current_task") or {}).get("task_id", "")
            own_files = {
                path
                for path, entry in (state.get("_file_history") or {}).items()
                if entry.get("task_id") == task_id
            }
            split = excuse_known_failures(
                report, known, own_files, test_commands=_test_commands(state)
            )
            if split:
                report.update(split)
                _emit_event(
                    state,
                    store,
                    {"event": "known_failures_excused", "iteration": state["iteration"], **split},
                )

        accepted = state.get("_accepted_test_ids") or []
        files = {node_file(nodeid) for nodeid in accepted if node_file(nodeid).endswith(".py")}
        sources = _read_workspace_files(Path(state["workspace_root"]), files)
        static = {item["file"] for item in removed_tests(accepted, sources, None)}
        missing = removed_tests(accepted, sources, report)
        # Tests a skipped task removed are excused (``_known_removals``) until
        # they are back, when they are protected again.
        known_removals = state.setdefault("_known_removals", {})
        gone = {f"{item['file']}::{name}" for item in missing for name in item["removed"]}
        for nodeid in [nodeid for nodeid in known_removals if nodeid not in gone]:
            del known_removals[nodeid]
        removed: list[dict[str, Any]] = []
        for item in missing:
            file = item["file"]
            names = [name for name in item["removed"] if f"{file}::{name}" not in known_removals]
            if names:
                removed.append(
                    {
                        "file": file,
                        "removed": names,
                        "owner_task": _accepted_owner(state, f"{file}::{names[0]}"),
                        # gone from the file on disk, or only missing from the run
                        "source": "static" if file in static else "sandbox",
                    }
                )
        if removed and removed != state.get("_removed_tests"):
            _emit_event(
                state,
                store,
                {"event": "tests_removed", "iteration": state["iteration"], "removed": removed},
            )
        state["_removed_tests"] = removed

    def _judge_review(self, state: AgentState, store: ArtifactStore) -> bool:
        """Decide whether the reviewed patch is approved; return True for an EMCS override.

        The orchestrator's own evidence (``_gate_issues``: removed tests,
        security findings that block in "block" mode, rejected patch paths)
        joins the review as issues, so it blocks approval and reaches the next
        implementer.  The reviewer's acceptance counts only
        under the hard gates (``should_accept``; untested code needs this explicit
        acceptance).  EMCS may override a rejection whose reasons are all minor,
        given real passing tests.  The reviewer's own verdict is never rewritten.
        A synthesized review (no reviewer call: it carries earlier issues
        forward) gets each gate issue once and is never scored or approved.
        """
        review = state.get("review_report")
        if not review:
            state["is_approved"] = False
            return False
        issues = list(review.get("issues") or [])
        review["issues"] = [*issues, *(i for i in self._gate_issues(state) if i not in issues)]
        if review.get("synthesized"):
            state["is_approved"] = False
            return False
        test_report = state.get("test_report")
        applied = state.get("patch_applied", False)
        threshold = self.config.pipeline.emcs_threshold

        reviewer_accepted = bool(review.get("acceptance_met"))
        accepted = reviewer_accepted and should_accept(
            review, test_report, threshold=threshold, patch_applied=applied, allow_untested=True
        )
        if reviewer_accepted and not accepted:
            logger.info("Reviewer accepted, but the hard gates failed — not approved")
        # Untested code is acceptable only with the reviewer's explicit acceptance.
        score = get_score_summary(
            review,
            test_report,
            patch_applied=applied,
            threshold=threshold,
            allow_untested=reviewer_accepted,
        )
        _emit_event(state, store, {"event": "emcs_score", "iteration": state["iteration"], **score})
        override = not accepted and can_override_rejection(
            review, test_report, threshold=threshold, patch_applied=applied
        )
        if override:
            logger.info("EMCS score %.2f meets threshold — approving", score["score"])
            _emit_event(
                state,
                store,
                {
                    "event": "emcs_override",
                    "iteration": state["iteration"],
                    "score": score["score"],
                    "threshold": threshold,
                    "issue_counts": score["issue_counts"],
                },
            )
        state["is_approved"] = accepted or override
        return override

    @staticmethod
    def _gate_issues(state: AgentState) -> list[dict[str, Any]]:
        """Review issues for orchestrator evidence that must block approval.

        Accepted tests that went missing, blocking security findings ("block"
        mode) and the patch paths the last apply rejected (nothing was written
        for them, so the patch is incomplete).
        """
        issues: list[dict[str, Any]] = [
            {
                "severity": "major",
                "title": f"Existing tests removed: {item['file']}",
                "details": ", ".join(item["removed"]),
                "recommended_fix": "",
                "evidence": item["file"],
                "fix_target": "test",
                "fix_file": item["file"],
                "source": "orchestrator",
            }
            for item in state.get("_removed_tests") or []
        ]
        issues.extend(
            {
                "severity": finding["severity"],
                "title": finding["title"],
                "details": finding["details"],
                "recommended_fix": "",
                "evidence": finding["file"],
                "fix_file": finding["file"] or None,
                "source": finding["source"],
            }
            for finding in state.get("_security_blockers") or []
        )
        # Paths the last apply refused (#110): nothing was written for them.
        issues.extend(
            {
                "severity": "blocker",
                "title": f"Rejected file path: {path}",
                "details": "unsafe path (absolute, drive/UNC, '..' or inside .git); not written",
                "recommended_fix": "",
                "evidence": path,
                "file": path,
                "source": "workspace",
            }
            for path in state.get("_rejected_paths") or []
        )
        return issues

    def _apply_or_turn_back(
        self,
        state: AgentState,
        store: ArtifactStore,
        workspace: Workspace,
        *,
        is_git_repo: bool,
    ) -> bool:
        """Apply the current patch, unless it goes straight back to the implementer.

        Turned back untested and unreviewed (``_turn_back``): a patch of only
        configuration files while the task has written no source
        (``config_only_patch``), a patch with blocking security findings in
        "block" mode (it is never written: ``security_blocked``), and a patch
        none of whose paths could be written (``paths_rejected``).  Returns
        whether the patch was applied and goes on to be tested.
        """
        config_files = self._config_only_files(state)
        if config_files:
            logger.warning("Implementer produced only config files (%s) — no source", config_files)
            self._turn_back(
                state,
                store,
                "config_only_patch",
                summary="The patch contains only configuration or metadata files.",
                stderr="Not tested: the patch was not applied.",
                issues=[
                    {
                        "severity": "blocker",
                        "title": "No source code produced",
                        "details": f"The patch only contains: {config_files}",
                        "recommended_fix": "",
                        "source": "orchestrator",
                    }
                ],
            )
            _emit_event(
                state,
                store,
                {
                    "event": "config_only_patch",
                    "iteration": state["iteration"],
                    "files": config_files,
                },
            )
            return False

        # Steps 2b-2d: security checks (constraints, secrets, dependencies)
        self._run_security_checks(state, store)
        if state.get("_security_blockers"):
            self._turn_back(
                state,
                store,
                "security_blocked",
                summary="The patch was not applied: it has blocking security findings.",
                stderr="Not tested: the patch was not applied (blocking security findings).",
            )
            return False

        # Step 2e: apply the patch to the workspace (and commit in a git repo)
        had_attempt = state.get("test_report") is not None
        self._apply_to_workspace(state, workspace, is_git_repo=is_git_repo, store=store)
        rejected = state.get("_rejected_paths") or []
        if rejected and not state.get("patch_applied"):
            self._turn_back(
                state,
                store,
                "paths_rejected",
                summary="No file was written: every path of the patch was rejected.",
                stderr=f"Not tested: no file was written; rejected path(s): {', '.join(rejected)}",
            )
            return False
        self._track_noop(state, store, had_attempt=had_attempt)
        return True

    @staticmethod
    def _config_only_files(state: AgentState) -> list[str]:
        """The files of a patch of only configuration while the task has written no source.

        Applies to tasks that expect source code: planned outputs that include
        source, or no planned outputs at all.  Once the task has written a
        source file, a later patch may legitimately change only configuration
        (e.g. a dependency).  Empty when the current patch is not such a patch.
        """
        files = (state.get("current_patch") or {}).get("files") or {}
        outputs = (state.get("current_task") or {}).get("outputs") or []
        expects_source = not outputs or any(map(_is_source_path, outputs))
        if (
            not files
            or any(map(_is_source_path, files))
            or not expects_source
            or any(map(_is_source_path, _task_files(state)))
        ):
            return []
        return sorted(files)

    @staticmethod
    def _turn_back(
        state: AgentState,
        store: ArtifactStore,
        reason: str,
        *,
        summary: str,
        stderr: str,
        issues: Iterable[dict[str, Any]] = (),
    ) -> None:
        """End the pass without applying, testing or reviewing the current patch.

        The pass gets a synthesized rejection: *issues* (the orchestrator's gate
        issues join them in ``_judge_review``), then the previous review's own
        issues, which still describe the unchanged workspace; the previous
        review is kept as ``previous_review``.  The pass's test report artifact
        records the skip (``skipped_reason`` *reason*, *stderr*), while
        ``test_report`` keeps the last real run.  Nothing changed in the
        workspace, so no-op tracking and the import warnings start over.
        """
        previous = state.get("review_report") or {}
        reviewer_issues = [
            copy.deepcopy(issue) for issue in previous.get("issues") or [] if "source" not in issue
        ]
        state["previous_review"] = state.get("review_report")
        state["review_report"] = {
            "summary": summary,
            "acceptance_met": False,
            "issues": [*issues, *reviewer_issues],
            "synthesized": True,
        }
        state["is_approved"] = False
        state["patch_applied"] = False
        state["patch_noop"] = False
        state["_noop_streak"] = 0
        state["_last_changed_files"] = []
        state["import_issues"] = []
        store.save_test_report(
            state["iteration"],
            {
                "passed": False,
                "total": 0,
                "failures": 0,
                "stdout": "",
                "stderr": stderr,
                "skipped_reason": reason,
            },
        )

    def _run_security_checks(self, state: AgentState, store: ArtifactStore) -> None:
        """Scan the current task's changes, the current patch included.

        The checks (constraints, secrets, dependencies) judge every file the
        run has changed against what it held before the run
        (``_security_baseline``), so a finding stays until its line is gone;
        the result is narrowed to the files of the current patch and of the
        current task.  ``security_findings`` (replaced every pass) goes to the
        agents unless the mode is "allow"; in "block" mode the blocker/major
        ones are ``_security_blockers`` (``security_blocked``): the patch is
        not applied and the task cannot be approved.  Each distinct finding is
        remembered for the summary (``_security_detected``).  A finding never
        ends the run.
        """
        patch = state.get("current_patch") or {}
        result = run_security_checks(
            patch,
            self._run_constraints,
            self.workspace_root,
            secrets_allowlist=self.config.security.secrets_allowlist,
            baseline=self._security_baseline,
        )
        result = _scoped_findings(result, {*touched_paths(patch), *_task_files(state)})
        state["messages"].extend(result.summary_events)
        for event in result.store_events:
            store.append_event(event)
        mode = self.config.pipeline.security_mode
        findings = [] if mode == "allow" else result.findings
        state["security_findings"] = findings
        state["_security_blockers"] = blocking_findings(result, mode)
        detected = state.setdefault("_security_detected", {})
        for finding in findings:
            entry = {key: finding.get(key) for key in ("source", "severity", "title", "file")}
            detected.setdefault("|".join(str(value) for value in entry.values()), entry)
        if state["_security_blockers"]:
            _emit_event(
                state,
                store,
                {
                    "event": "security_blocked",
                    "iteration": state["iteration"],
                    "findings": [
                        {"title": f["title"], "severity": f["severity"], "file": f["file"]}
                        for f in state["_security_blockers"]
                    ],
                },
            )

    def _record_cost(
        self,
        cost_tracker: CostTracker,
        state: AgentState,
        role: str,
        meta: dict[str, Any],
        *,
        generation_seconds: float,
        gpu_watts: float | None,
    ) -> None:
        """Book one agent call and update the run's cumulative ``total_cost_usd``.

        Whether the call was local comes from the role's endpoint, not the model name.
        """
        role_config = self._role_config(role)
        cost_tracker.record(
            meta.get("model", "unknown"),
            meta.get("input_tokens", 0),
            meta.get("output_tokens", 0),
            request_id=meta.get("request_id", ""),
            provider_request_id=meta.get("provider_request_id", ""),
            role=role,
            iteration=state["iteration"],
            generation_seconds=generation_seconds,
            gpu_watts=gpu_watts,
            is_local=is_local_model(role_config) if isinstance(role_config, RoleConfig) else None,
            cache_read_tokens=int(meta.get("cache_read_tokens") or 0),
            cache_write_tokens=int(meta.get("cache_write_tokens") or 0),
            temperature=meta.get("temperature"),
        )
        state["total_cost_usd"] = cost_tracker.cumulative_cost_usd

    def _effective_roles(self) -> dict[str, dict[str, Any]]:
        """Each role's provider, model, endpoint and resolved prompt version (no keys)."""
        return {
            role: {
                "provider": role_config.provider,
                "model": role_config.model,
                "base_url": role_config.base_url,
                "prompt_version": resolve_prompt_version(role_config),
            }
            for role in ROLE_NAMES
            if (role_config := self._role_config(role)) is not None
        }

    def _role_config(self, role: str) -> RoleConfig | None:
        """The model configuration *role* runs on (the director uses the architect's)."""
        role_config = getattr(self.config, _CONFIG_ROLES.get(role, role), None)
        return role_config if isinstance(role_config, RoleConfig) else None

    def _create_architect(self) -> ArchitectAgent:
        return create_architect(self.config)

    def _create_implementer(self) -> ImplementerAgent:
        return create_implementer(self.config)

    def _create_reviewer(self) -> ReviewerAgent:
        return create_reviewer(self.config)

    def _create_director(self) -> DirectorAgent:
        return create_director(self.config)

    async def _invoke_agent(
        self,
        agent: ArchitectAgent | ImplementerAgent | ReviewerAgent | DirectorAgent,
        input_data: dict[str, Any],
        role: str,
    ) -> dict[str, Any]:
        """Invoke an agent, using streaming when enabled.

        When ``self._stream`` is true, uses the agent's ``stream_invoke``
        method and emits ``token`` step-callback events for each chunk.
        Otherwise falls back to the standard ``invoke`` call.

        Before calling the agent, unloads other Ollama models from VRAM
        so the active model has enough GPU memory (critical for 12GB cards
        running multi-model presets like local).

        Returns the validated artifact dict (with ``_meta``).
        """
        # Free VRAM for the model about to be used
        role_config = self._role_config(role)
        if role_config and role_config.base_url:
            unload_except(self.config, role_config.model)

        if getattr(self, "_stream", False):
            self._notify_step(f"{role}_start", {})
            result: dict[str, Any] | None = None
            async for item in agent.stream_invoke(input_data):
                if isinstance(item, dict):
                    result = item
                else:
                    # str token chunk — relay to callback
                    self._notify_step("token", {"text": item, "role": role})
            if result is None:
                raise ValueError(f"Streaming {role} ended without producing an artifact")
            return result
        return await agent.invoke(input_data)

    async def _call_agent(
        self,
        agent: ArchitectAgent | ImplementerAgent | ReviewerAgent | DirectorAgent,
        input_data: dict[str, Any],
        role: str,
        cost_tracker: CostTracker,
        state: AgentState,
        *,
        store: _EventSink | None = None,
        selection: ContextSelection | None = None,
    ) -> dict[str, Any]:
        """Invoke *agent* and account for the call; return the artifact without ``_meta``.

        The request is recorded as LLM egress before it is sent (a failed call
        has still sent the prompt), and the call runs within the run's wall
        time (``_within_deadline``).  One cost row is booked per call, with the
        GPU power of a local role sampled right after its generation; a call
        whose output never validated (``AgentOutputError``, e.g. truncated
        twice) is booked from the usage of its attempts before it is re-raised.
        Input the agent trimmed to fit its context window is reported
        (``_report_trim``); workspace files it dropped from a prompt built from
        *selection* then count as not shown (``selection.omitted``).
        """
        role_config = self._role_config(role)
        if role_config is None:
            raise ValueError(f"No model configuration for role {role!r}")
        self._net_guard.record_llm_call(
            role=role,
            provider=role_config.provider,
            base_url=role_config.base_url,
            model=role_config.model,
        )
        start = time.monotonic()
        try:
            result = await self._within_deadline(
                role, lambda: self._invoke_agent(agent, input_data, role)
            )
        except AgentOutputError as e:
            if e.meta.get("attempts"):
                await self._account_call(
                    state, store, role, e.meta, cost_tracker, start=start, selection=selection
                )
            raise
        meta: dict[str, Any] = result.pop("_meta", {})
        await self._account_call(
            state, store, role, meta, cost_tracker, start=start, selection=selection
        )
        return result

    async def _account_call(
        self,
        state: AgentState,
        store: _EventSink | None,
        role: str,
        meta: Mapping[str, Any],
        cost_tracker: CostTracker,
        *,
        start: float,
        selection: ContextSelection | None,
    ) -> None:
        """Book one agent call from its ``_meta`` (cost row, GPU sample) and report its trim."""
        generation_seconds = time.monotonic() - start
        role_config = self._role_config(role)
        local = role_config is not None and is_local_model(role_config)
        gpu_watts = await asyncio.to_thread(get_gpu_power_watts) if local else None
        if meta.get("context_trimmed"):
            self._report_trim(state, store, role, _trim_report(meta["context_trimmed"]), selection)
        self._record_cost(
            cost_tracker,
            state,
            role,
            dict(meta),
            generation_seconds=generation_seconds,
            gpu_watts=gpu_watts,
        )

    def _report_trim(
        self,
        state: AgentState,
        store: _EventSink | None,
        role: str,
        report: dict[str, Any],
        selection: ContextSelection | None,
    ) -> None:
        """Report a prompt the context guard trimmed: ``context_trimmed`` (events, step, log).

        The event carries the trimmed keys and, as the guard reports them, the
        window, the token counts before and after, the dropped paths and
        whether the prompt is still over its budget.
        """
        event = {
            "event": "context_trimmed",
            "role": role,
            "iteration": state["iteration"],
            **report,
        }
        logger.info("%s prompt trimmed to fit its context window: %s", role, report["keys"])
        self._emit_step_event(state, store, event)
        if selection is not None:
            _omit_dropped(selection, _dropped_paths(report))
            _mark_excerpted(selection, _excerpted_paths(report))

    async def _context_budget(self, role: str, agent: object) -> int:
        """Bytes of workspace files to show *role*, derived from its context window.

        The window is the role's ``context_window`` or what its agent detects
        (0 = unknown: ``DEFAULT_FILE_BUDGET``).  Of the input tokens left beside
        the response reserve, the system prompt and the output schema (as
        ``BaseAgent`` budgets them), files get ``FILE_BUDGET_SHARE``; the result
        lies between ``MIN_FILE_BUDGET`` and ``DEFAULT_FILE_BUDGET``.
        """
        window = await self._context_window(role, agent)
        if window <= 0:
            return DEFAULT_FILE_BUDGET
        reserve = max(MIN_OUTPUT_RESERVE_TOKENS, window // 4)
        free_tokens = window - reserve - _prompt_overhead_tokens(agent)
        budget = int(max(free_tokens, 0) * CHARS_PER_TOKEN * FILE_BUDGET_SHARE)
        return min(DEFAULT_FILE_BUDGET, max(MIN_FILE_BUDGET, budget))

    async def _context_window(self, role: str, agent: object) -> int:
        """The context window of *role* in tokens (0 = unknown).

        The role's ``context_window`` when configured, else what its agent detects.
        """
        role_config = self._role_config(role)
        if role_config is not None and role_config.context_window > 0:
            return role_config.context_window
        detect = (
            agent.context_window
            if isinstance(agent, BaseAgent)
            else getattr(agent, "_resolve_context_window", None)
        )
        if detect is None:
            return 0
        try:
            return int(await detect() or 0)
        except Exception:
            logger.debug("Context window detection failed for %s", role, exc_info=True)
            return 0

    async def _run_architect(
        self,
        state: AgentState,
        architect: ArchitectAgent,
        cost_tracker: CostTracker,
        store: ArtifactStore | None = None,
    ) -> AgentState:
        """Run the architect agent to create the plan, or re-plan the remaining work.

        The architect sees the file tree and a repo map ranked for the request;
        a re-plan also gets the completed tasks, the current task and the
        architecture issues that asked for it (``_architect_input``).  The plan
        is normalized (unique task ids, known dependencies, test outputs; see
        ``normalize_plan``; changes are logged and emitted as
        ``plan_normalized``) before any task is selected from it; once tasks
        are completed no task is merged into another, so pending work cannot
        vanish into a finished task.  A failed first plan, or one without a
        task, ends the run; a re-plan is adopted by ``_adopt_replan`` (a failed
        or rejected one keeps the current plan).
        """
        logger.info("Running architect agent...")
        state["messages"].append({"event": "architect_starting"})
        replanning = state.get("plan") is not None
        input_data = self._architect_input(state, replanning=replanning)

        try:
            raw_plan = await self._call_agent(
                architect, input_data, "architect", cost_tracker, state, store=store
            )
            result, plan_warnings = normalize_plan(
                raw_plan,
                state["file_tree"],
                merge_redundant=not state.get("_completed_tasks"),
            )
        except WallTimeExceeded:
            raise
        except Exception as e:
            _note_truncation(state, store, "architect", e)
            if replanning:
                logger.warning("Re-planning failed; keeping the current plan: %s", e)
                _emit_event(
                    state,
                    store,
                    {"event": "replan_failed", "iteration": state["iteration"], "error": str(e)},
                )
                return state
            state["status"] = "error"
            state["error"] = f"Architect failed: {e}"
            state["messages"].append({"event": "architect_error", "error": str(e)})
            return state

        if plan_warnings:
            logger.warning("Plan normalized: %s", "; ".join(map(str, plan_warnings)))
            self._emit_step_event(
                state, store, {"event": "plan_normalized", "warnings": plan_warnings}
            )
        tasks = result.get("tasks", [])
        if replanning:
            if not await self._adopt_replan(state, result, store):
                return state
        elif not tasks:
            state["status"] = "error"
            state["error"] = "Architect produced a plan with no tasks"
            state["messages"].append({"event": "architect_error", "error": state["error"]})
            return state
        else:
            state["plan"] = result
            _init_task_plan(state, tasks)
        state["messages"].append(
            {
                "event": "architect_complete",
                "summary": result.get("summary", ""),
                "task_count": len(tasks),
                "cost_usd": cost_tracker.cumulative_cost_usd,
            }
        )
        logger.info("Plan created: %s (%d tasks)", result.get("summary", ""), len(tasks))
        return state

    def _architect_input(self, state: AgentState, *, replanning: bool) -> dict[str, Any]:
        """Architect input: the request, project, file tree and repo map, and the constraints.

        A re-plan adds the completed tasks (with the files they wrote), the
        tasks given up (``skipped_tasks``: with why, see ``_skipped_tasks_input``),
        the task being worked on (``current_task``: id and title) and only the
        review issues that asked for it (``fix_target`` "architecture"), plus
        the reason of an orchestrator-side request (``replan_request``, e.g. a
        director ruling).
        """
        input_data: dict[str, Any] = {
            "request": state["request"],
            "project_info": state["project_info"],
            "file_tree": state["file_tree"],
        }
        repo_map = gather_repo_map(
            Path(state["workspace_root"]),
            task={"description": state["request"]},
            file_tree=state["file_tree"],
            languages=state["project_info"].get("languages"),
        )
        if repo_map:
            input_data["repo_map"] = repo_map
        constraints = self._constraints_input()
        if constraints:
            input_data["constraints"] = constraints
        if replanning:
            completed = set(state.get("_completed_tasks") or [])
            history = state.get("_file_history") or {}
            completed_tasks = [
                {
                    "task_id": task_id,
                    "title": task.get("title", ""),
                    "outputs": task.get("outputs") or [],
                    "files": _files_of_task(history, task_id),
                }
                for task in state.get("all_tasks") or []
                if (task_id := task.get("task_id", "")) in completed
            ]
            if completed_tasks:
                input_data["completed_tasks"] = completed_tasks
            skipped_tasks = _skipped_tasks_input(state)
            if skipped_tasks:
                input_data["skipped_tasks"] = skipped_tasks
            current = state.get("current_task")
            if current:
                input_data["current_task"] = {
                    "task_id": current.get("task_id", ""),
                    "title": current.get("title", ""),
                }
            issues = architecture_issues(state.get("review_report"))
            request = state.get("replan_request") or {}
            if request.get("reason"):
                source = request.get("source") or "orchestrator"
                issues = [
                    *issues,
                    {
                        "severity": "major",
                        "title": f"Re-plan requested by the {source}",
                        "details": request["reason"],
                        "fix_target": "architecture",
                    },
                ]
            if issues:
                input_data["review_feedback"] = issues
        return input_data

    async def _adopt_replan(
        self, state: AgentState, plan: dict[str, Any], store: ArtifactStore | None
    ) -> bool:
        """Install a re-plan once it has pending work and, in interactive mode, approval.

        Nothing changes before that: a plan without a pending task, or one the
        user rejects, is ``replan_rejected`` (``reason`` "no_pending_task" or
        "user") and the run goes on with the current plan.  Returns whether the
        plan was installed (``_install_replan``).
        """
        task_ids = [task.get("task_id", "") for task in plan.get("tasks") or []]
        completed, skipped = _replan_finished_ids(state, plan.get("tasks") or [])
        finished = {*completed, *skipped}
        if all(task_id in finished for task_id in task_ids):
            reason = "no_pending_task"
        elif not await self._announce_plan(state, plan):
            reason = "user"
        else:
            self._install_replan(state, plan, store)
            return True
        logger.warning("Re-plan rejected (%s); keeping the current plan", reason)
        _emit_event(
            state,
            store,
            {
                "event": "replan_rejected",
                "iteration": state["iteration"],
                "reason": reason,
                "task_ids": task_ids,
            },
        )
        return False

    def _install_replan(
        self, state: AgentState, plan: dict[str, Any], store: ArtifactStore | None
    ) -> None:
        """Make a re-plan the plan of the run, keeping the progress it still describes.

        Completed tasks whose ids the new plan keeps stay finished, and so do
        skipped ones it keeps unchanged; a skipped task it reworks under its
        old id is pending again (``_reopen_task``).  The first pending task
        becomes current with fresh per-task state, and pre-generated patches
        are dropped.  The plan's iteration budget starts now, for the tasks it
        leaves to do (``_plan_iteration_base``).  Other known failures stay: the
        code that left them failing is still on the run branch.  The plan is
        saved as ``work_plan_r<n>.json`` (``work_plan.json`` keeps the first
        one) and ``replanned`` is emitted with it.
        """
        tasks: list[dict[str, Any]] = plan.get("tasks") or []
        task_ids = [task.get("task_id", "") for task in tasks]
        completed = state.get("_completed_tasks") or []
        kept, skipped = _replan_finished_ids(state, tasks)
        reworked = [
            task_id
            for task_id in state.get("_skipped_tasks") or []
            if task_id in task_ids and task_id not in skipped
        ]
        for task_id in reworked:
            _reopen_task(state, task_id)
        state["plan"] = plan
        state["_completed_tasks"] = kept
        state["_skipped_tasks"] = skipped
        state["_parallel_patches"] = {}
        _init_task_plan(state, tasks)
        current = cast(dict[str, Any], state["current_task"])
        _reset_for_next_task(state, current)
        finished_budget = self.config.pipeline.max_iterations * (len(kept) + len(skipped))
        state["_plan_iteration_base"] = max(0, state["iteration"] - finished_budget)
        self._observer.reset()
        revision = state.get("_replan_count", 0)
        _save_json_artifact(store, f"work_plan_r{revision}.json", plan)
        _emit_event(
            state,
            store,
            {
                "event": "replanned",
                "iteration": state["iteration"],
                "replan": revision,
                "kept_completed": kept,
                "dropped_completed": [task_id for task_id in completed if task_id not in kept],
                "reworked_skipped": reworked,
                "current_task": current.get("task_id", ""),
                "plan": plan,
            },
        )

    async def _replan(
        self,
        state: AgentState,
        architect: ArchitectAgent,
        cost_tracker: CostTracker,
        store: ArtifactStore,
        workspace: Workspace,
    ) -> AgentState:
        """Re-run the architect for the remaining work (an architecture issue asked for it).

        Counts the attempt (``_replan_count``, capped by the router) and
        refreshes the file tree the architect sees.  The request (``_replan``)
        is consumed once the architect has answered, so a run stopped during
        the re-plan asks again when it resumes.
        """
        state["_replan_count"] = state.get("_replan_count", 0) + 1
        state["file_tree"] = workspace.get_file_tree()
        mark = _StepMark.take(cost_tracker, state)
        state = await self._run_architect(state, architect, cost_tracker, store)
        self._observe_step("architect", state, cost_tracker, mark, state.get("plan"))
        state["_replan"] = False
        state["replan_request"] = None
        return state

    async def _announce_plan(self, state: AgentState, plan: dict[str, Any]) -> bool:
        """Show a plan (architect reasoning, ``architect_complete`` step); return whether approved.

        In interactive mode the user must approve it (``plan_approved``);
        otherwise it is approved as shown.
        """
        tasks = plan.get("tasks", [])
        # Emit architect reasoning for live dashboard display
        state["messages"].append(
            {
                "event": "architect_reasoning",
                "summary": plan.get("summary", ""),
                "tasks": [
                    {
                        "title": t.get("title", ""),
                        "description": t.get("description", ""),
                        "risk_level": t.get("risk_level", "low"),
                    }
                    for t in tasks
                ],
                "assumptions": plan.get("assumptions", []),
                "termination": plan.get("termination", {}),
            }
        )
        self._notify_step(
            "architect_complete",
            {"summary": plan.get("summary", ""), "task_count": len(tasks)},
        )
        if not self.config.pipeline.interactive:
            return True
        approved = await self._interactive_approve_plan(plan)
        if approved:
            state["messages"].append({"event": "plan_approved"})
            logger.info("Plan approved by user")
        else:
            logger.info("Plan rejected by user")
        return approved

    async def _run_implementer(
        self,
        state: AgentState,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
        store: _EventSink | None = None,
    ) -> AgentState:
        """Run the implementer agent to generate code.

        A task's first attempt uses the patch generated for it in parallel
        mode, if any (``_take_parallel_patch``); otherwise the agent is called
        (``_generate_patch``).  A successful call consumes the director's
        guidance and its directed attempt.  A failed call ends the attempt with
        a synthesized review (``_agent_failed``).
        """
        logger.info("Running implementer agent (iteration %d)...", state["iteration"])
        pregenerated = self._take_parallel_patch(state, store)
        result = (
            pregenerated
            if pregenerated is not None
            else await self._generate_patch(state, implementer, cost_tracker, store)
        )
        if result is None:
            return state

        (state.get("_agent_failures") or {}).pop("implementer", None)
        state["_director_guidance"] = []
        state["_arbitration_pending"] = False
        state["current_patch"] = result
        state["messages"].append(
            {
                "event": "implementer_complete",
                "status": result.get("status", "ok"),
                "rationale": result.get("rationale", ""),
                "cost_usd": cost_tracker.cumulative_cost_usd,
                "pregenerated": pregenerated is not None,
            }
        )
        if result.get("status") == "blocked":
            state["status"] = "error"
            state["error"] = "Implementer is blocked: " + "; ".join(
                result.get("questions", ["Unknown reason"])
            )
        logger.info("Patch generated: %s", result.get("rationale", "")[:100])
        return state

    async def _generate_patch(
        self,
        state: AgentState,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
        store: _EventSink | None,
    ) -> dict[str, Any] | None:
        """Call the implementer for the current task; None when the call failed.

        The workspace files shown fit the implementer's context window
        (``_context_budget``).  A local model writes a task's first patch in
        two passes while there is no feedback yet (``_two_pass_capable``):
        pass 1 writes the source code, pass 2 the tests, with that code and
        the existing tests of those modules as context, so the tests call what
        really exists.  Each
        response is resolved against what it was shown (``_resolve_patch``):
        its ``edits`` become whole ``files``, and files that would overwrite
        content the implementer never saw whole are dropped.  The dropped
        paths become ``_blocked_paths`` (``patch_entry_blocked``).
        """
        budget = await self._context_budget("implementer", implementer)
        input_data, selection = self._build_implementer_input(state, budget)
        use_two_pass = (
            self._two_pass_capable()
            and state.get("_task_iteration", 0) == 0
            and not any(input_data.get(key) for key in _FEEDBACK_KEYS)
        )
        try:
            if use_two_pass:
                result, blocked = await self._run_two_pass(
                    state,
                    implementer,
                    cost_tracker,
                    input_data,
                    budget=budget,
                    selection=selection,
                    store=store,
                )
            else:
                result = await self._call_agent(
                    implementer,
                    input_data,
                    "implementer",
                    cost_tracker,
                    state,
                    store=store,
                    selection=selection,
                )
                blocked = self._resolve_patch(state, result, selection)
        except WallTimeExceeded:
            raise
        except Exception as e:
            self._agent_failed(state, store, "implementer", e)
            return None
        self._block_paths(state, store, blocked)
        return result

    def _two_pass_capable(self) -> bool:
        """Whether the implementer writes first patches in two passes (code, then tests).

        Only a local implementer whose prompt explains the two modes
        (``TWO_PASS_PROMPT_VERSION`` or later, as resolved for the role).
        """
        implementer = self.config.implementer
        return (
            is_local_base_url(implementer.base_url)
            and resolve_prompt_version(implementer) >= TWO_PASS_PROMPT_VERSION
        )

    def _take_parallel_patch(
        self, state: AgentState, store: _EventSink | None
    ) -> dict[str, Any] | None:
        """The patch generated in parallel for the current task's first attempt, if usable.

        It was generated from the run branch as it was then; when a task applied
        since then changed one of its paths (written, deleted or diffed), the
        patch is stale: it is dropped, each such path becomes a "File not
        written" blocker for the live call that replaces it, and
        ``parallel_patch_conflict`` is emitted.
        """
        if state.get("_task_iteration", 0):
            return None
        task_id = (state.get("current_task") or {}).get("task_id", "")
        entry = (state.get("_parallel_patches") or {}).pop(task_id, None)
        if entry is None:
            return None
        patch: dict[str, Any] = entry["patch"]
        history = state.get("_file_history") or {}
        changed_by: dict[str, str] = {}
        for path in _patch_paths(patch):
            record = history.get(normalize_rel_path(path) or "") or {}
            if record.get("task_id") not in (None, task_id) and (
                record.get("iteration", 0) >= entry["iteration"]
            ):
                changed_by[path] = record["task_id"]
        if changed_by:
            logger.warning("Parallel patch of %s is stale (%s); regenerating", task_id, changed_by)
            state["_blocked_paths"] = [
                {
                    "file": path,
                    "reason": f"task {owner} changed it after this patch was generated in parallel",
                }
                for path, owner in changed_by.items()
            ]
            _emit_event(
                state,
                store,
                {
                    "event": "parallel_patch_conflict",
                    "iteration": state["iteration"],
                    "task_id": task_id,
                    "paths": changed_by,
                },
            )
            return None
        state["_blocked_paths"] = entry["blocked_paths"]
        return patch

    async def _run_two_pass(
        self,
        state: AgentState,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
        input_data: dict[str, Any],
        *,
        budget: int,
        selection: ContextSelection,
        store: _EventSink | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Pass 1 writes the code, pass 2 the tests for it; return the merged patch.

        Each pass is resolved against its own context (``_resolve_patch``):
        pass 1 against *selection* (files the context guard dropped from its
        prompt count as not shown), pass 2 against ``_pass_two_selection``:
        pass 1's code first (the tests must call it), then the existing tests
        of the modules it touches, then the files pass 1 saw, within *budget*;
        the code is marked ``_protected_files`` for the context guard.  Pass 2
        may only rewrite existing files it was shown whole: other test modules
        keep their tests (``_preserve_existing_tests``), other files (fixtures,
        helpers) are dropped.  Returns the patch and the dropped paths.
        """
        task = dict(input_data.get("task") or {})
        task_id = task.get("task_id", "")
        self._notify_step("implementer_pass", {"pass": 1, "mode": "code_only", "task_id": task_id})
        result = await self._call_agent(
            implementer,
            {**input_data, "task": {**task, "mode": "code_only"}},
            "implementer",
            cost_tracker,
            state,
            store=store,
            selection=selection,
        )
        blocked = self._resolve_patch(state, result, selection)
        pass1_files: dict[str, str] = result["files"]
        code_files = {p: c for p, c in pass1_files.items() if not is_test_path(p)}

        # Pass 2 sees the new code and the existing tests of the modules it touches.
        root = Path(state["workspace_root"])
        context = self._pass_two_selection(state, code_files, selection, budget)
        pass2_input = {k: v for k, v in input_data.items() if k not in _SELECTION_INPUT_KEYS}
        pass2_input.update({k: v for k, v in _selection_input(context).items() if v})
        self._notify_step("implementer_pass", {"pass": 2, "mode": "tests_only", "task_id": task_id})
        test_result = await self._call_agent(
            implementer,
            {
                **pass2_input,
                "task": {**task, "mode": "tests_only"},
                "_protected_files": sorted(code_files),
            },
            "implementer",
            cost_tracker,
            state,
            store=store,
            selection=context,
        )
        unseen_tests = self._unseen_test_modules(root, test_result.get("files") or {}, context)
        blocked += self._resolve_patch(state, test_result, context, tests_only=True)

        # Merge: code from pass 1, tests and new support files from pass 2
        merged_files, dropped = _merge_two_pass(pass1_files, test_result["files"], root)
        if dropped:
            logger.warning("Pass 2 rewrote existing files; dropped: %s", dropped)
            _emit_event(
                state,
                store,
                {
                    "event": "implementer_pass2_dropped",
                    "paths": dropped,
                    "iteration": state["iteration"],
                },
            )
        result["files"] = self._preserve_existing_tests(
            state, merged_files, store, unseen=unseen_tests
        )
        tests_rationale = test_result.get("rationale", "")
        if tests_rationale:
            result["rationale"] = f"{result.get('rationale', '')}\n\nTests: {tests_rationale}"
        return result, blocked

    def _pass_two_selection(
        self,
        state: AgentState,
        code_files: Mapping[str, str],
        selection: ContextSelection,
        budget: int,
    ) -> ContextSelection:
        """What pass 2 is shown: pass 1's code, then its modules' tests, then pass 1's files.

        Within *budget* bytes, the code always (``_fit_files``).  Each shown
        file is whole or an excerpt (``partial``) as its source has it; the
        relevant files that did not fit are ``omitted``.
        """
        module_tests = select_context(
            Path(state["workspace_root"]),
            {"outputs": list(code_files)},
            None,
            state.get("file_tree", []),
            budget,
        )
        # (files, excerpted files' on-disk sizes), in the order pass 2 sees them
        sources: list[tuple[Mapping[str, str], Mapping[str, int]]] = [
            (code_files, {}),
            (module_tests.files, module_tests.partial),
            (selection.files, selection.partial),
        ]
        shown = _fit_files([files for files, _ in sources], budget, keep=code_files)
        partial: dict[str, int] = {}
        omitted = {**selection.omitted, **module_tests.omitted}
        for files, excerpts in sources:
            for path, text in files.items():
                if path in shown:
                    if path in excerpts and path not in partial and shown[path] == text:
                        partial[path] = excerpts[path]
                else:
                    omitted.setdefault(path, excerpts.get(path, len(text.encode("utf-8"))))
        return ContextSelection(
            files=shown,
            partial=partial,
            omitted={path: size for path, size in omitted.items() if path not in shown},
            binary={**selection.binary, **module_tests.binary},
        )

    @staticmethod
    def _unseen_test_modules(
        root: Path, files: Mapping[str, str], context: ContextSelection
    ) -> list[str]:
        """Existing pytest modules among *files* that the response was not shown whole."""
        return [
            path
            for path in files
            if is_pytest_file(path)
            and (path not in context.files or path in context.partial)
            and (rel := normalize_rel_path(path)) is not None
            and (root / rel).is_file()
        ]

    def _build_implementer_input(
        self, state: AgentState, budget: int = DEFAULT_FILE_BUDGET
    ) -> tuple[dict[str, Any], ContextSelection]:
        """Implementer input for the current task, and the file selection it shows.

        Evidence about the last attempt (failing tests only when the run really
        failed, without the known failures, which come separately with their
        origin; flaky and removed tests, a previous attempt that changed
        nothing), the code (relevant files within *budget* bytes, the files of
        earlier and dependency tasks, the on-disk size of each file shown only
        as an excerpt, the relevant files too large or binary to show, the repo
        map) and the current task's feedback: review issues,
        nondeterministic tests this run wrote, director rulings, import warnings
        when the last run failed, paths not written.  The files the attempt must
        edit are listed as ``_protected_files`` for the context guard.  Retries
        that would repeat themselves get the retry temperature.
        """
        root = Path(state["workspace_root"])
        task = state.get("current_task")
        report = state.get("test_report")
        failed_run = report if report and classify_test_evidence(report) == "failed" else None
        import_warnings = (state.get("import_issues") or []) if failed_run else []
        feedback = self._implementer_feedback(state)
        cited = [*feedback, *import_warnings] or None  # the files they name are shown
        prior = _prior_files(state)
        file_tree = state.get("file_tree", [])
        selection = select_context(
            root,
            task,
            cited,
            file_tree,
            budget,
            prior_files=prior,
            test_report=failed_run,
        )
        repo_map = gather_repo_map(
            root,
            task,
            cited,
            file_tree,
            (state.get("project_info") or {}).get("languages"),
            prior_files=prior,
        )
        input_data: dict[str, Any] = {
            "task": task,
            "plan_summary": (state.get("plan") or {}).get("summary", ""),
            "request": state.get("request", ""),
            "project_info": state["project_info"],
            "framework_hints": build_framework_hints(self.workspace_root),
        }
        evidence: dict[str, Any] = {
            "failing_tests": _failing_tests_text(failed_run) if failed_run else None,
            "baseline_failures": _known_failure_origins(
                state, (report or {}).get("baseline_failures") or ()
            ),
            "flaky_tests": (report or {}).get("flaky_tests"),
            "removed_tests": state.get("_removed_tests"),
            **_selection_input(selection),
            "repo_map": repo_map,
            "previous_attempt": self._previous_attempt(state),
            "director_guidance": state.get("_director_guidance"),
            "security_findings": state.get("security_findings"),
            "import_warnings": import_warnings,
            "review_feedback": feedback,
            "constraints": self._constraints_input(),
            "_protected_files": _protected_files(state, feedback, selection),
        }
        input_data.update({key: value for key, value in evidence.items() if value})
        reason = self._retry_reason(state)
        if reason is not None:
            temperature = self._retry_temperature(state)
            logger.info("Implementer retry temperature %.2f (reason=%s)", temperature, reason)
            input_data["_temperature"] = temperature
        return input_data, selection

    @staticmethod
    def _implementer_feedback(state: AgentState) -> list[dict[str, Any]]:
        """The current task's review issues, plus the facts the next attempt must act on.

        A reviewer rejection that itemized nothing itself (the orchestrator's
        gate issues aside) becomes one issue carrying its summary.  Added: the
        gate issues the review does not carry (``_gate_issues``, e.g. the paths
        the last apply rejected), the files of the last response that were not
        written, and one ``nondeterministic_test`` item per known flaky test
        this run wrote (``_known_flaky_tests``, owner "this_run"; also when an
        earlier task wrote it).  The review is reset when the task changes, so
        review issues never cross a task boundary.
        """
        review = state.get("review_report") or {}
        feedback: list[dict[str, Any]] = list(review.get("issues") or [])
        gate_issues = Pipeline._gate_issues(state)
        reviewer_issues = [issue for issue in feedback if issue not in gate_issues]
        if review and not (
            reviewer_issues or review.get("acceptance_met") or review.get("synthesized")
        ):
            feedback.append(
                {
                    "severity": "major",
                    "title": "Reviewer rejected without itemized issues",
                    "details": review.get("summary", ""),
                    "source": "orchestrator",
                }
            )
        feedback.extend([issue for issue in gate_issues if issue not in feedback])
        # Files of the last response that would have overwritten unseen content
        feedback.extend(
            {
                "severity": "blocker",
                "title": f"File not written: {item['file']}",
                "details": item["reason"],
                "file": item["file"],
                "source": "orchestrator",
            }
            for item in state.get("_blocked_paths") or []
        )
        # Nondeterministic tests the run wrote (structured facts; prompts hold the policy)
        feedback.extend(
            {
                "severity": "major",
                "category": "nondeterministic_test",
                "title": f"nondeterministic_test: {record['test']}",
                "file": record["file"],
                "test": record["test"],
                "owner_task": record["owner_task"],
                "evidence": record["evidence"],
                "source": "orchestrator",
            }
            for record in (state.get("_known_flaky_tests") or {}).values()
            if record.get("owner") == "this_run"
        )
        return feedback

    @staticmethod
    def _previous_attempt(state: AgentState) -> dict[str, Any] | None:
        """What the last attempt did when its patch changed nothing (else None)."""
        if not state.get("_noop_streak"):
            return None
        patch = state.get("current_patch") or {}
        return {
            "no_changes": True,
            "files": sorted([*(patch.get("files") or {}), *(patch.get("delete_files") or [])]),
            "still_failing": sorted(failing_test_ids(state.get("test_report")) or ()),
        }

    @staticmethod
    def _retry_reason(state: AgentState) -> str | None:
        """Why the next attempt risks repeating the last one, if it does.

        "noop" after a patch that changed nothing, "repeated_failure" when the
        task's last two runs failed alike (``failures_recur``), "directed" for the attempt a
        director ruling directs (it must not fall back into the old patch).
        """
        last_two = (state.get("_failure_signatures") or [])[-2:]
        if state.get("_noop_streak"):
            return "noop"
        if len(last_two) == 2 and failures_recur(last_two[1], last_two[0]):
            return "repeated_failure"
        if state.get("_director_guidance"):
            return "directed"
        return None

    def _retry_temperature(self, state: AgentState) -> float | None:
        """Sampling temperature for an attempt that would otherwise repeat the last one.

        When there is a ``_retry_reason``: the configured retry temperature,
        never below the role's own.  Otherwise None (the role's temperature
        applies).
        """
        if self._retry_reason(state) is None:
            return None
        return max(self.config.implementer.temperature, self.config.pipeline.retry_temperature)

    def _resolve_patch(
        self,
        state: AgentState,
        result: dict[str, Any],
        selection: ContextSelection,
        *,
        tests_only: bool = False,
    ) -> list[dict[str, str]]:
        """Resolve a response against the context it was shown; return the paths dropped.

        A ``files`` entry that would lose content the implementer never saw is
        dropped (``_unseen_overwrite``).  Each edit is then applied to the
        file's current workspace text (``Workspace.render_edits``) and the
        whole result joins ``files``; ``edits`` is emptied, so every later step
        sees whole files.  A file whose edits do not all apply is not written.
        """
        root = Path(state["workspace_root"])
        files: dict[str, str] = result.get("files") or {}
        blocked = [
            {"file": path, "reason": reason}
            for path in files
            if (reason := self._unseen_overwrite(path, selection, root, tests_only=tests_only))
        ]
        dropped = {item["file"] for item in blocked}
        kept = {path: content for path, content in files.items() if path not in dropped}
        edits = result.get("edits") or []
        if edits:
            rendered = Workspace(repo_path=root, run_id=self.run_id).render_edits(edits)
            kept.update(rendered.files)
            blocked += [
                {"file": path, "reason": f"edits not applied: {error}"}
                for path, error in rendered.errors.items()
            ]
        result["files"] = kept
        result["edits"] = []
        return blocked

    @staticmethod
    def _unseen_overwrite(
        path: str, selection: ContextSelection, root: Path, *, tests_only: bool = False
    ) -> str:
        """Why rewriting *path* whole would lose content that was never shown ("" if it would not).

        A binary file, or an existing file left out (``omitted``, also when the
        context guard dropped it from the prompt) or shown only as an excerpt
        (``partial``), changes only through ``edits``.  A *tests_only* pass
        may not rewrite any existing file it was not shown whole (a fixture or
        helper other tests use), pytest modules aside: ``_preserve_existing_tests``
        keeps their tests.
        """
        if path in selection.binary:
            return "binary file; it was not shown"
        rel = normalize_rel_path(path)
        if tests_only and rel is not None and is_pytest_file(rel):
            return ""
        if path in selection.omitted:
            return "existing file that was not shown"
        if path in selection.partial:
            return "existing file that was shown only as an excerpt"
        if (
            tests_only
            and rel is not None
            and path not in selection.files
            and (root / rel).is_file()
        ):
            return "existing file that was not shown"
        return ""

    @staticmethod
    def _block_paths(
        state: AgentState, store: _EventSink | None, blocked: list[dict[str, str]]
    ) -> None:
        """Record the response files that were not written (``_blocked_paths``, next feedback).

        Emits ``patch_entry_blocked`` when there are any.
        """
        state["_blocked_paths"] = blocked
        if not blocked:
            return
        logger.warning("Not writing %s", blocked)
        _emit_event(
            state,
            store,
            {"event": "patch_entry_blocked", "iteration": state["iteration"], "paths": blocked},
        )

    def _agent_failed(
        self, state: AgentState, store: _EventSink | None, role: str, error: Exception
    ) -> None:
        """Handle an implementer or reviewer call that failed (invalid output, timeout).

        The attempt fails instead of the run (``<role>_unavailable``): an
        implementer failure drops the attempt's patch and keeps the previous
        review's issues; a reviewer failure leaves an issue-free rejection.
        Both are synthesized reviews.  The role's
        ``MAX_CONSECUTIVE_AGENT_FAILURES``-th failure in a row on the task ends
        the run with ``status="error"``.  Every failure emits ``<role>_error``.
        """
        failures = state.setdefault("_agent_failures", {})
        failures[role] = count = failures.get(role, 0) + 1
        message = f"{role.capitalize()} failed: {error}"
        logger.warning("%s (%d in a row)", message, count)
        _note_truncation(state, store, role, error)
        event = {
            "event": f"{role}_error",
            "error": str(error),
            "iteration": state["iteration"],
            "consecutive_failures": count,
        }
        _emit_event(state, store, event)
        if count >= MAX_CONSECUTIVE_AGENT_FAILURES:
            state["status"] = "error"
            state["error"] = message
            return
        # Not fatal: the attempt goes on without this role's output.
        _emit_event(state, store, {**event, "event": f"{role}_unavailable"})
        issues: list[dict[str, Any]] = []
        if role == "implementer":
            state["current_patch"] = None
            state["previous_review"] = state.get("review_report")
            issues = copy.deepcopy((state.get("review_report") or {}).get("issues") or [])
        state["review_report"] = {
            "summary": message,
            "acceptance_met": False,
            "issues": issues,
            "synthesized": True,
        }

    def _preserve_existing_tests(
        self,
        state: AgentState,
        files: dict[str, str],
        store: _EventSink | None = None,
        *,
        unseen: Collection[str] = (),
    ) -> dict[str, str]:
        """Keep a tests-only pass from overwriting tests that are already on disk.

        A Python test file the current task did not write is never replaced by a
        version that drops some of its tests, nor by any version when the pass
        was not shown it whole (*unseen*): only the genuinely new tests go to a
        sibling file (``tests/test_x_<task_id>.py``), or nowhere when there are
        none.  Each redirect emits ``tests_preserved``.
        """
        task_id = (state.get("current_task") or {}).get("task_id", "")
        history = state.get("_file_history") or {}
        candidates = [
            path
            for path in files
            if path.endswith(".py")
            and is_test_path(path)
            and (history.get(normalize_rel_path(path) or "") or {}).get("task_id") != task_id
        ]
        on_disk = _read_workspace_files(Path(state["workspace_root"]), candidates)
        kept = dict(files)
        for path, old in on_disk.items():
            existing = extract_test_functions(old) if old is not None else set()
            dropped = sorted(existing - extract_test_functions(files[path]))
            if not dropped and not (path in unseen and old is not None):
                continue
            del kept[path]
            redirected_to = None
            new_tests = strip_known_tests(files[path], existing)
            if new_tests is not None:
                redirected_to = sibling_test_path(path, task_id)
                kept[redirected_to] = new_tests
            logger.warning("Kept %s: the rewrite dropped %s", path, dropped)
            _emit_event(
                state,
                store,
                {
                    "event": "tests_preserved",
                    "file": path,
                    "dropped_tests": dropped,
                    "redirected_to": redirected_to,
                    "iteration": state["iteration"],
                },
            )
        return kept

    def _constraints_input(self) -> dict[str, Any] | None:
        """The project constraints for agent input, normalized; None when there are none.

        A pattern table whose names are its patterns (the list form) is shown
        as the list it was written as.
        """
        merged = merge_constraints({}, self.config.pipeline.constraints)
        return {
            key: list(value)
            if isinstance(value, dict) and all(k == v for k, v in value.items())
            else value
            for key, value in merged.items()
        } or None

    @staticmethod
    def _ensure_tests(patch: dict[str, Any], languages: Collection[str] = ()) -> dict[str, Any]:
        """Add synthetic smoke tests (``SMOKE_TEST_PATH``) to a patch that has no test file.

        Only for Python projects (or when no language is known): other test runners
        would never execute the Python smoke file.  A patch whose only Python
        tests are ones pytest does not collect (Django's ``tests.py``, conftest
        or fixture modules) still gets them, so pytest does not exit with "no
        tests ran".  Returns a new patch dict.
        """
        files = dict(patch.get("files") or {})
        is_python = not languages or "python" in {lang.lower() for lang in languages}
        has_tests = any(
            is_pytest_file(path) or (is_test_path(path) and not path.endswith(".py"))
            for path in files
        )
        if not files or not is_python or has_tests:
            return patch

        # Find Python source files to test
        source_files = [
            f for f in files if f.endswith(".py") and "__init__" not in f and not is_test_path(f)
        ]

        if not source_files:
            # Non-Python files only — generate a file-existence test
            lines = ['"""Auto-generated smoke test."""\n', "import os\n\n"]
            for f in files:
                safe_name = f.replace("/", "_").replace("\\", "_").replace(".", "_")
                lines.append(f"def test_{safe_name}_exists():\n")
                lines.append(f'    assert os.path.exists("{f}"), "{f} should exist"\n\n')
            files[SMOKE_TEST_PATH] = "\n".join(lines)
        else:
            # Python source files — generate syntax + existence tests
            # Avoid direct imports since GUI apps (tkinter) block on import
            lines = ['"""Auto-generated smoke tests."""\n', "import ast\nimport os\n"]
            for src in source_files:
                safe = src.replace("/", "_").replace("\\", "_").replace(".", "_").replace("-", "_")
                lines.append(f"\ndef test_{safe}_exists():\n")
                lines.append(f'    assert os.path.exists("{src}"), "{src} should exist"\n\n')
                lines.append(f"def test_{safe}_valid_syntax():\n")
                lines.append(f'    with open("{src}") as f:\n')
                lines.append("        source = f.read()\n")
                lines.append("    ast.parse(source)  # raises SyntaxError if invalid\n\n")
            files[SMOKE_TEST_PATH] = "\n".join(lines)
            logger.info("Auto-generated import tests for %d source files", len(source_files))

        return {**patch, "files": files}

    # Languages that can be tested in Docker (Linux container)
    _SANDBOX_TESTABLE: ClassVar[set[str]] = {
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "java",
        "kotlin",
        "ruby",
        "php",
        "csharp",
        "c#",
        "fsharp",
        "f#",
        "powershell",
        "shell",
        "c++",
    }

    @staticmethod
    def _sandbox_languages(state: AgentState) -> set[str]:
        """Languages of the project and of the current patch's files (lowercase)."""
        project_info = state.get("project_info") or {}
        languages = {lang.lower() for lang in project_info.get("languages", [])}
        patch_files = (state.get("current_patch") or {}).get("files") or {}
        for f in patch_files:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if ext in ("ps1", "psm1", "bat", "cmd"):
                languages.add("powershell")
            elif ext in ("cpp", "cc", "cxx", "h", "hpp"):
                languages.add("c++")
            elif ext in ("cs",):
                languages.add("c#")
            elif ext in ("fs", "fsx"):
                languages.add("f#")
            elif ext in ("swift",):
                languages.add("swift")
            elif ext in ("sh", "bash", "zsh"):
                languages.add("shell")
        return languages

    def _can_sandbox_test(self, state: AgentState) -> bool:
        """Check if the project's languages can be tested in Docker (unknown: yes)."""
        languages = self._sandbox_languages(state)
        return not languages or bool(languages & self._SANDBOX_TESTABLE)

    def _parallel_mode(self, state: AgentState, store: ArtifactStore, workspace: Workspace) -> bool:
        """Whether ready tasks get their first patches generated concurrently (``parallel_tasks``).

        Needs the run branch: those patches are generated in git worktrees of
        its HEAD.  Without one, ``parallel_disabled`` is emitted and every task
        runs sequentially.
        """
        if not self.config.pipeline.parallel_tasks:
            return False
        if workspace.branch:
            return True
        logger.warning("Parallel tasks need a git workspace; running tasks sequentially")
        _emit_event(
            state,
            store,
            {"event": "parallel_disabled", "reason": "the workspace is not a git repository"},
        )
        return False

    @staticmethod
    def _parallel_siblings(state: AgentState) -> list[dict[str, Any]]:
        """Tasks whose first patch can be generated beside the current task's live call.

        The other ready tasks (dependencies finished) without a generated patch,
        when the current task starts its first attempt with a live call and the
        run branch holds every applied change (worktrees branch from its HEAD).
        """
        current_id = (state.get("current_task") or {}).get("task_id")
        generated = state.get("_parallel_patches") or {}
        if (
            state.get("_task_iteration", 0)
            or current_id in generated
            or state.get("workspace_commit_error")
        ):
            return []
        return [
            task
            for task in _ready_tasks(state.get("all_tasks") or [], _finished_task_ids(state))
            if task.get("task_id") not in (current_id, *generated)
        ]

    async def _run_parallel_implementers(
        self,
        state: AgentState,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
        store: ArtifactStore,
        siblings: list[dict[str, Any]],
    ) -> AgentState:
        """Run the current task's implementer while its ready siblings' first patches are generated.

        Each sibling runs concurrently in its own git worktree of the run
        branch's HEAD (which holds every finished task's code), on a copy of the
        state set up as that task's first attempt.  Its patch waits in
        ``_parallel_patches`` until the task becomes current; it is then applied,
        tested, reviewed and gated like any other patch.  A sibling whose
        worktree or implementer call fails, or whose implementer is blocked, is
        reported (``parallel_task_fallback``; a failed call also counts as an
        observer error) and implemented normally when its turn comes.
        """
        worktrees = WorktreeManager(repo_path=self.workspace_root, run_id=self.run_id)
        sibling_states = [self._sibling_state(state, task) for task in siblings]
        self._notify_step(
            "parallel_start",
            {
                "current_task": (state.get("current_task") or {}).get("task_id", ""),
                "tasks": [task.get("task_id", "") for task in siblings],
            },
        )
        try:
            current, *generated = await asyncio.gather(
                self._run_implementer(state, implementer, cost_tracker, store),
                *(
                    self._pregenerate(sibling, worktrees, implementer, cost_tracker, store)
                    for sibling in sibling_states
                ),
                return_exceptions=True,
            )
        finally:
            with contextlib.suppress(Exception):
                worktrees.cleanup_all()
        if isinstance(current, BaseException):
            raise current

        state["total_cost_usd"] = cost_tracker.cumulative_cost_usd
        stash = state.setdefault("_parallel_patches", {})
        fallback: list[str] = []
        for task, outcome in zip(siblings, generated, strict=True):
            task_id = task.get("task_id", "")
            if isinstance(outcome, dict):
                stash[task_id] = {**outcome, "iteration": state["iteration"]}
                continue
            fallback.append(task_id)
            error = str(outcome)
            logger.warning("Parallel generation for %s failed (%s); it runs later", task_id, error)
            if isinstance(outcome, _SiblingCallFailed):
                # A failed agent call, like any other (its usage is in the step's).
                self._observer.record_agent_call(
                    "implementer", error=error, input_tokens=0, output_tokens=0, calls=0
                )
            _emit_event(
                state,
                store,
                {
                    "event": "parallel_task_fallback",
                    "iteration": state["iteration"],
                    "task_id": task_id,
                    "error": error,
                },
            )
        self._notify_step(
            "parallel_complete",
            {
                "generated": [t.get("task_id", "") for t in siblings if t.get("task_id") in stash],
                "fallback": fallback,
            },
        )
        return state

    @staticmethod
    def _sibling_state(state: AgentState, task: dict[str, Any]) -> AgentState:
        """A copy of *state* set up as *task*'s first attempt (its own audit trail).

        It holds no patch, so a failed call can never return the current task's.
        """
        sibling = cast(
            AgentState,
            copy.deepcopy({key: value for key, value in state.items() if key != "messages"}),
        )
        sibling["messages"] = []
        sibling["current_patch"] = None
        _reset_for_next_task(sibling, task)
        return sibling

    async def _pregenerate(
        self,
        state: AgentState,
        worktrees: WorktreeManager,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
        store: ArtifactStore,
    ) -> dict[str, Any]:
        """Generate a sibling task's first patch in a worktree: ``{"patch", "blocked_paths"}``.

        Its events reach ``events.jsonl`` tagged with its task (``_SiblingEvents``).
        Raises when the worktree failed, when the implementer call failed
        (``_SiblingCallFailed``) or when the implementer is blocked: the task
        is then implemented live when its turn comes, as in sequential mode.
        """
        task_id = (state.get("current_task") or {}).get("task_id", "")
        key = worktree_key(task_id)
        try:
            state["workspace_root"] = str(worktrees.workspace_path(worktrees.create(key)))
            state = await self._run_implementer(
                state, implementer, cost_tracker, _SiblingEvents(store, task_id)
            )
        finally:
            with contextlib.suppress(Exception):
                worktrees.cleanup(key)
        patch = state.get("current_patch")
        if patch is None:
            raise _SiblingCallFailed((state.get("review_report") or {}).get("summary", "no patch"))
        if patch.get("status") == "blocked":
            raise RuntimeError(state.get("error") or "Implementer is blocked")
        return {"patch": patch, "blocked_paths": state.get("_blocked_paths") or []}

    async def _maybe_arbitrate(
        self, state: AgentState, cost_tracker: CostTracker, store: ArtifactStore
    ) -> None:
        """Step 4c: let the director rule on a conflict that recurs on the current task.

        Each recurring failure (``_recurring_conflicts``) is arbitrated once per
        task, and recurring review titles once per task at all: titles churn, so
        each new combination would call the director again.  The rulings update
        the review (``apply_arbitration``) and are saved as
        ``arbitration_00N.json``; REVISE rulings become ``_director_guidance``
        for the next attempt, which the stall policy grants
        (``_arbitration_pending``) only when a REVISE ruling matched one of the
        conflicts.  A director failure is logged (``arbitration_failed``) and
        changes nothing.
        """
        review = state.get("review_report")
        if (
            not self.config.pipeline.enable_arbitration
            or not review
            or review.get("synthesized")
            or state.get("is_approved")
        ):
            return
        conflicts, key = self._recurring_conflicts(state, review)
        arbitrated = state.setdefault("_arbitrated_signatures", [])
        if not conflicts or not _may_arbitrate(key, arbitrated):
            return
        arbitrated.append(key)
        titles = [conflict.get("title", "") for conflict in conflicts]
        logger.info("Arbitration triggered for %d conflicts", len(conflicts))
        _emit_event(
            state,
            store,
            {
                "event": "arbitration_triggered",
                "iteration": state["iteration"],
                "conflicts": titles,
            },
        )

        mark = _StepMark.take(cost_tracker, state)
        try:
            result = await self._call_agent(
                self._create_director(),
                self._director_input(state, conflicts),
                "director",
                cost_tracker,
                state,
                store=store,
            )
            ruling = ArbitrationReport.model_validate(result)
        except WallTimeExceeded:
            raise
        except Exception as e:
            logger.warning("Director arbitration failed: %s", e)
            _emit_event(
                state,
                store,
                {"event": "arbitration_failed", "iteration": state["iteration"], "error": str(e)},
            )
            self._observe_step("director", state, cost_tracker, mark, None)
            return
        resolutions = [resolution.model_dump() for resolution in ruling.resolutions]
        self._observe_step("director", state, cost_tracker, mark, {"resolutions": resolutions})

        apply_arbitration(cast(dict[str, Any], state), resolutions)
        revisions = [r for r in resolutions if r["decision"] == "REVISE"]
        ruled = {normalize_title(conflict.get("title")) for conflict in conflicts}
        state["_director_guidance"] = revisions
        state["_arbitration_pending"] = any(
            normalize_title(revision.get("title")) in ruled for revision in revisions
        )
        # A ruling that the plan itself must change asks for a re-plan (capped
        # by pipeline.max_replans like the reviewer's architecture issues).
        replans = [r for r in revisions if r.get("fix_target") == "architecture"]
        if replans:
            state["replan_request"] = {
                "source": "director",
                "reason": replans[0].get("reasoning") or replans[0].get("title", ""),
            }
        _save_json_artifact(
            store,
            f"arbitration_{state['iteration']:03d}.json",
            {
                "iteration": state["iteration"],
                "task_id": (state.get("current_task") or {}).get("task_id", ""),
                "conflict_key": key,
                "conflicts": conflicts,
                "resolutions": resolutions,
                "approved": state["is_approved"],
            },
        )
        _emit_event(
            state,
            store,
            {
                "event": "arbitration_resolved",
                "iteration": state["iteration"],
                "conflicts": titles,
                "resolutions": resolutions,
                "approved": state["is_approved"],
                "directed_attempt": state["_arbitration_pending"],
            },
        )

    def _recurring_conflicts(
        self, state: AgentState, review: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str]:
        """The current task's recurring conflicts and their identity (``conflict_key``).

        ``detect_conflict`` on this attempt (``_task_iteration`` is incremented
        after arbitration) and the task's last two failure signatures.  The
        orchestrator's own gate issues are evidence, never arbitrated: a
        recurring failure whose only blocking issues are gate issues is the
        failure itself.
        """
        signatures = state.get("_failure_signatures") or []
        current = signatures[-1] if signatures else ""
        previous = signatures[-2] if len(signatures) > 1 else ""
        conflicts = detect_conflict(
            review,
            state.get("previous_review"),
            state.get("_task_iteration", 0) + 1,
            current_signature=current,
            previous_signature=previous,
            exclude=self._gate_issues(state),
        )
        key = conflict_key(conflicts, current_signature=current, previous_signature=previous)
        return conflicts, key

    def _director_input(self, state: AgentState, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
        """The director's evidence: the conflicts, the task, the test run and the task's files.

        The failing-test excerpt also shows the previous attempt's failure, so
        the director sees whether both sides of an assertion moved.
        """
        report = state.get("test_report") or {}
        evidence = classify_test_evidence(report)
        files = _read_workspace_files(Path(state["workspace_root"]), _task_files(state))
        data = build_arbitration_input(
            state["request"],
            state.get("plan"),
            conflicts,
            (state.get("current_patch") or {}).get("rationale", ""),
            evidence == "passed",
            task=state.get("current_task"),
            failing_tests=report_excerpt(report) if evidence == "failed" else "",
            changed_files={path: text for path, text in files.items() if text is not None},
            test_evidence=evidence,
        )
        excerpts = state.get("_failure_excerpts") or []
        previous = excerpts[-2] if len(excerpts) > 1 else ""
        if data.get("failing_tests") and previous:
            data["failing_tests"] = _with_previous_failure(data["failing_tests"], previous)
        return data

    def _observe_step(
        self,
        role: str,
        state: AgentState,
        cost_tracker: CostTracker,
        mark: _StepMark,
        output: dict[str, Any] | None,
    ) -> None:
        """Report an agent step to the observer: the calls and tokens it booked, and its failure.

        Usage is the cost tracker's growth since *mark*, so two-pass and
        parallel calls count and a step without a provider call counts 0; the
        step failed when it logged one of its ``_FAILURE_EVENTS``.  One observer
        event per step.
        """
        usage = mark.usage(cost_tracker)
        error = next(
            (
                str(message.get("error") or message["event"])
                for message in state["messages"][mark.messages :]
                if message.get("event") in _FAILURE_EVENTS[role]
            ),
            None,
        )
        if role == "reviewer" and error is None and output is not None:
            self._observer.record_review(output, **usage)
        else:
            self._observer.record_agent_call(
                role, output=None if error else output, error=error, **usage
            )

    async def _run_sandbox(
        self,
        state: AgentState,
        sandbox: BaseSandbox,
        store: ArtifactStore | None = None,
    ) -> AgentState:
        """Run tests in the sandbox. Skips for non-testable languages.

        A skipped run is reported as not passed, with ``skipped_reason``: it is
        no test evidence, so only explicit reviewer acceptance can approve it.
        A run whose only tests are the synthetic smoke tests is marked
        ``synthetic_tests`` for the same reason.  The sandbox tests the
        workspace as applied: its ``diff_unified`` never goes to the sandbox
        (the workspace applied it, or ignored it beside whole ``files``), and
        a diff that did not apply is not tested at all.
        """
        original = state.get("current_patch") or {}
        diff_only = bool(original.get("diff_unified")) and not original.get("files")
        if diff_only and not state.get("patch_applied"):
            logger.warning("The diff did not apply to the workspace; nothing to test")
            state["test_report"] = {
                "passed": False,
                "total": 0,
                "failures": 0,
                "stdout": "",
                "stderr": "The diff did not apply to the workspace; nothing was tested.",
            }
            _emit_event(
                state,
                store,
                {
                    "event": "sandbox_skipped",
                    "reason": "patch_not_applied",
                    "iteration": state["iteration"],
                },
            )
            return state

        # Skip sandbox for languages that can't run in Docker Linux containers
        # BUT: always run if user explicitly selected a Docker image
        has_custom_image = bool(self.config.pipeline.docker_image)
        if not has_custom_image and not self._can_sandbox_test(state):
            languages = sorted(self._sandbox_languages(state))
            logger.info("Skipping sandbox — language(s) %s not testable in Docker", languages)
            state["test_report"] = {
                "passed": False,
                "total": 0,
                "failures": 0,
                "stdout": "",
                "stderr": (
                    f"Sandbox skipped: {', '.join(languages)} cannot be tested "
                    "in a Docker Linux container."
                ),
                "skipped_reason": "non_testable_language",
            }
            state["messages"].append(
                {
                    "event": "sandbox_skipped",
                    "reason": "non_testable_language",
                    "languages": languages,
                    "iteration": state["iteration"],
                }
            )
            return state

        sandbox_type = type(sandbox).__name__
        logger.info("Running sandbox tests (%s)...", sandbox_type)

        # The workspace copy already holds the applied patch.  Smoke tests and
        # run options go to the sandbox only, never into the stored patch.
        staged = {**original, "diff_unified": ""} if original.get("diff_unified") else original
        languages = (state.get("project_info") or {}).get("languages") or []
        patch = self._ensure_tests(staged, languages)
        smoke = patch["files"][SMOKE_TEST_PATH] if patch is not staged else None

        # Emit sandbox info if available
        sandbox_info: dict[str, Any] = getattr(sandbox, "get_info", lambda: {})()
        state["messages"].append(
            {
                "event": "sandbox_starting",
                "sandbox_type": sandbox_type,
                "iteration": state["iteration"],
                **sandbox_info,
            }
        )

        try:
            sandbox_start = time.monotonic()
            self._net_guard.record_sandbox_call(sandbox_type=self.config.pipeline.sandbox)
            options = self._sandbox_options(state, store)
            result = await self._within_deadline(
                "sandbox", lambda: sandbox.apply_and_test({**patch, **options})
            )
            sandbox_ms = (time.monotonic() - sandbox_start) * 1000

            # Extract sandbox metadata before storing test report
            sandbox_meta = result.pop("_sandbox", {})
            if smoke is not None and result.get("total", 0) <= len(extract_test_functions(smoke)):
                result["synthetic_tests"] = True

            state["test_report"] = result
            state["messages"].append(
                {
                    "event": "sandbox_complete",
                    "sandbox_type": sandbox_type,
                    "passed": result.get("passed", False),
                    "total": result.get("total", 0),
                    "failures": result.get("failures", 0),
                    "failed_tests": result.get("failed_tests") or [],
                    "duration_ms": round(sandbox_ms),
                    "entrypoint_runs": _entrypoint_summary(result.get("entrypoint_runs") or []),
                    **sandbox_meta,
                }
            )

            logger.info(
                "Tests: %s (%d total, %d failures)",
                "PASSED" if result.get("passed") else "FAILED",
                result.get("total", 0),
                result.get("failures", 0),
            )
            if result.get("passed_with_flaky"):
                # Opt-in tolerance: every failure was a pre-existing flake that
                # passed on a rerun; ``raw_passed`` keeps the real result.
                _emit_event(
                    state,
                    store,
                    {
                        "event": "flaky_tolerated",
                        "iteration": state["iteration"],
                        "failed_tests": result.get("failed_tests") or [],
                        "flaky_tests": result.get("flaky_tests") or [],
                    },
                )

        except WallTimeExceeded:
            raise
        except Exception as e:
            state["test_report"] = {
                "passed": False,
                "total": 0,
                "failures": 0,
                "stderr": str(e),
            }
            state["messages"].append({"event": "sandbox_error", "error": str(e)})

        return state

    def _sandbox_options(
        self, state: AgentState, store: ArtifactStore | None = None
    ) -> dict[str, Any]:
        """Per-run sandbox options: flaky-test rerun, targeted tests, the scripts to smoke-run.

        The patch's ``tests_to_run`` that call the project's own test runner run
        first as ``targeted_tests``; the others, and any entry holding a line
        break or another control character (it would end the shell command
        early), are reported (``targeted_tests_rejected``) and not run.
        """
        options = self._flaky_options(state)
        requested: list[str] = (state.get("current_patch") or {}).get("tests_to_run") or []
        if requested:
            single_line = [command for command in requested if not _has_control_chars(command)]
            targeted, rejected = select_targeted_tests(single_line, _test_commands(state))
            rejected = [*(c for c in requested if c not in single_line), *rejected]
            if targeted:
                options["targeted_tests"] = targeted
            if rejected:
                _emit_event(
                    state,
                    store,
                    {
                        "event": "targeted_tests_rejected",
                        "iteration": state["iteration"],
                        "commands": rejected,
                    },
                )
        entrypoints = self._entrypoints(state) if self.config.pipeline.entrypoints_enabled() else []
        if entrypoints:
            options["entrypoints"] = entrypoints
        return options

    def _flaky_options(self, state: AgentState) -> dict[str, Any]:
        """Sandbox options for flaky tests: rerun still-failing tests, tolerate pre-existing flakes.

        ``run_files`` (every path the run wrote or deleted so far) lets the
        sandbox refuse to tolerate a flaky test the run itself wrote.
        """
        pipeline = self.config.pipeline
        return {
            "flaky_rerun": pipeline.flaky_rerun,
            "flaky_reruns": pipeline.flaky_reruns,
            "tolerate_flaky": pipeline.tolerate_flaky_tests,
            "tolerate_preexisting_flaky": pipeline.tolerate_flaky_tests,
            "run_files": sorted(state.get("_file_history") or {}),
        }

    @staticmethod
    def _entrypoints(state: AgentState) -> list[str]:
        """The current task's Python scripts to smoke-run.

        Only where the sandbox runs Python: a Python project, or one whose
        primary language has no toolchain image of its own (the default image
        is Python's; e.g. a Python project that also ships a shell script).
        """
        task = state.get("current_task")
        languages = (state.get("project_info") or {}).get("languages") or []
        primary = languages[0] if languages else ""
        if not task or (primary != "python" and select_docker_image(primary)):
            return []
        return select_entrypoints(task, _task_files(state), Path(state["workspace_root"]))

    def _sandbox_spec(self, project_info: ProjectInfo) -> dict[str, Any]:
        """``create_sandbox`` arguments for the detected project."""
        pipeline = self.config.pipeline
        return {
            "sandbox_type": pipeline.sandbox,
            "workspace_root": self.workspace_root,
            "test_commands": project_info.test_commands or [PYTEST_COMMAND],
            "install_commands": get_install_commands(project_info, self.workspace_root),
            "docker_image": pipeline.docker_image,
            "primary_language": project_info.languages[0] if project_info.languages else "",
            "run_entrypoints": pipeline.run_entrypoints,
        }

    def _reconfigure_sandbox(
        self,
        state: AgentState,
        store: ArtifactStore,
        spec: dict[str, Any],
        sandbox: BaseSandbox,
    ) -> tuple[dict[str, Any], BaseSandbox]:
        """Re-detect the project after a patch changed files; re-create the sandbox if needed.

        ``project_info`` is refreshed.  When the toolchain the sandbox was built
        for (language, test or install commands) changed — e.g. a first
        ``package.json`` in a new project — a new sandbox (image) is created
        and the old one cleaned up.  An empty project's first Python file only
        names the language the default (Python) sandbox already had, so it
        keeps the sandbox.  Returns the sandbox spec and sandbox to use.
        """
        if not state.get("_last_changed_files"):
            return spec, sandbox
        project_info = detect_project(self.workspace_root)
        state["project_info"] = _project_info_dict(project_info)
        new_spec = self._sandbox_spec(project_info)
        if _same_sandbox(new_spec, spec):
            return spec, sandbox
        logger.info(
            "Detected toolchain changed (%s) — re-creating the sandbox",
            new_spec["primary_language"] or "unknown",
        )
        _emit_event(
            state,
            store,
            {
                "event": "sandbox_reconfigured",
                "iteration": state["iteration"],
                "language": new_spec["primary_language"],
                "test_commands": new_spec["test_commands"],
                "install_commands": new_spec["install_commands"],
            },
        )
        replacement = create_sandbox(**new_spec)
        _cleanup_sandbox(sandbox)
        return new_spec, replacement

    def _record_patch(self, state: AgentState, store: ArtifactStore) -> None:
        """Save the implementer's patch artifact and announce it."""
        patch = state.get("current_patch") or {}
        store.save_patch(state["iteration"], patch)
        store.append_event(
            {
                "event": "patch_saved",
                "iteration": state["iteration"],
                "status": patch.get("status", "ok"),
            }
        )
        # Emit implementer reasoning for live dashboard display
        state["messages"].append(
            {
                "event": "implementer_reasoning",
                "iteration": state["iteration"],
                "rationale": patch.get("rationale", ""),
                "status": patch.get("status", "ok"),
                "files_changed": list(patch.get("files") or {}),
                "tests_to_run": patch.get("tests_to_run", []),
                "questions": patch.get("questions", []),
            }
        )
        self._notify_step(
            "implementer_complete",
            {"iteration": state["iteration"], "status": patch.get("status", "ok")},
        )

    @staticmethod
    def _track_noop(state: AgentState, store: ArtifactStore, *, had_attempt: bool) -> None:
        """Count the task's consecutive attempts whose patch changed nothing (``_noop_streak``).

        A task's first attempt never counts: there is nothing it could repeat.
        """
        if not (had_attempt and state.get("patch_noop")):
            state["_noop_streak"] = 0
            return
        streak = state["_noop_streak"] = state.get("_noop_streak", 0) + 1
        patch = state.get("current_patch") or {}
        files = sorted([*(patch.get("files") or {}), *(patch.get("delete_files") or [])])
        logger.warning("Attempt changed no files (%d in a row)", streak)
        _emit_event(
            state,
            store,
            {
                "event": "noop_patch",
                "iteration": state["iteration"],
                "task_id": (state.get("current_task") or {}).get("task_id", ""),
                "files": files,
                "streak": streak,
            },
        )

    @staticmethod
    def _record_test_outcomes(state: AgentState, store: ArtifactStore) -> None:
        """Fold the latest run into the test history, the flaky tests and the attempt history.

        Nondeterministic tests are those the sandbox saw pass on a rerun (the
        report's ``flaky_tests``; evidence "passed_on_rerun") and those whose
        outcome flipped while nothing that could explain it changed since
        their previous run (``flaky_from_history``; "flip_on_unchanged_tree";
        added to the report's ``flaky_tests``); a failing test the sandbox
        reran without a pass is no such flip.  Each is attributed
        (``_flaky_record``: written by this run or not) and kept in
        ``_known_flaky_tests``, across tasks, until its test file changes after
        the detection; ``_flaky_seen`` keeps the run's first record of each
        for good (``resolved_iteration``: when its file changed after it).
        ``flaky_tests_detected`` is emitted per kind of evidence.  The task's
        failure signature (known flaky tests left out) and its failure excerpt
        are appended to its attempt history.
        """
        report = state.get("test_report")
        if report is None:
            return
        outcomes = _test_outcomes(report)
        history = state.setdefault("_test_history", {})
        file_history = state.get("_file_history") or {}
        iteration = state["iteration"]
        rerun: list[str] = list(report.get("flaky_tests") or [])
        rerun_failed = (
            [nodeid for nodeid in report.get("failed_tests") or [] if nodeid not in rerun]
            if report.get("rerun_attempts")
            else []
        )
        flipped = [
            nodeid
            for nodeid in flaky_from_history(
                outcomes,
                history,
                file_history,
                workspace_root=Path(state["workspace_root"])
                if state.get("workspace_root")
                else None,
                rerun_failed=rerun_failed,
            )
            if nodeid not in rerun
        ]
        report["flaky_tests"] = [*rerun, *flipped]
        known = state.setdefault("_known_flaky_tests", {})
        seen = state.setdefault("_flaky_seen", {})
        for nodeid, record in list(known.items()):
            seen.setdefault(nodeid, dict(record))  # known before _flaky_seen was kept
            changed = (file_history.get(record["file"]) or {}).get("iteration", 0)
            if changed > record["detected_iteration"]:
                del known[nodeid]
                seen[nodeid]["resolved_iteration"] = changed
        for evidence, tests in (("passed_on_rerun", rerun), ("flip_on_unchanged_tree", flipped)):
            if not tests:
                continue
            records = [_flaky_record(nodeid, evidence, file_history, iteration) for nodeid in tests]
            known.update((record["test"], record) for record in records)
            for record in records:
                first = seen.setdefault(record["test"], dict(record))
                first.pop("resolved_iteration", None)  # nondeterministic again
            logger.warning("Nondeterministic tests (%s): %s", evidence, tests)
            _emit_event(
                state,
                store,
                {
                    "event": "flaky_tests_detected",
                    "iteration": iteration,
                    "tests": tests,
                    "evidence": evidence,
                    "records": records,
                },
            )
        _merge_test_history(history, outcomes, iteration)
        state.setdefault("_failure_signatures", []).append(_task_failure_signature(report, known))
        failed = classify_test_evidence(report) == "failed"
        excerpt = report_excerpt(report)[:MAX_FAILURE_EXCERPT] if failed else ""
        state["_failure_excerpts"] = [*(state.get("_failure_excerpts") or []), excerpt][-2:]

    def _record_test_report(self, state: AgentState, store: ArtifactStore) -> None:
        """Save the test report artifact and announce it."""
        report = state.get("test_report") or {}
        if report:
            store.save_test_report(state["iteration"], report)
            store.append_event(
                {
                    "event": "test_report_saved",
                    "iteration": state["iteration"],
                    "passed": report.get("passed", False),
                    "failed_tests": report.get("failed_tests") or [],
                    "flaky_tests": report.get("flaky_tests") or [],
                }
            )
        self._notify_step(
            "tests_complete",
            {"iteration": state["iteration"], "passed": report.get("passed", False)},
        )

    @staticmethod
    def _carry_review_forward(state: AgentState) -> None:
        """Keep the previous review for an attempt that changed nothing and still fails.

        The reviewer is not called: it would judge unchanged code.  The
        synthesized review drops issues citing a test file none of whose tests
        fails any more (that failure did not recur).
        """
        previous = state.get("review_report") or {}
        failing = failing_test_ids(state.get("test_report"))
        failing_files = {node_file(nodeid) for nodeid in failing or ()}
        issues = [
            copy.deepcopy(issue)
            for issue in previous.get("issues") or []
            if failing is None or not _cites_passing_test(issue, failing_files)
        ]
        logger.info("Attempt changed nothing and still fails — previous review stands")
        state["previous_review"] = state.get("review_report")
        state["review_report"] = {
            "summary": "No files changed; the previous review stands.",
            "acceptance_met": False,
            "issues": issues,
            "synthesized": True,
        }

    async def _run_reviewer(
        self,
        state: AgentState,
        reviewer: ReviewerAgent,
        cost_tracker: CostTracker,
        store: ArtifactStore | None = None,
    ) -> AgentState:
        """Run the reviewer agent.

        Only produces the review; whether it approves the patch is decided by
        ``_judge_review`` (the reviewer's opinion is advisory).  The related
        files shown fit the reviewer's context window (``_context_budget``).
        A failed call ends the attempt with a synthesized review
        (``_agent_failed``).
        """
        logger.info("Running reviewer agent...")

        # Save previous review for stall detection
        state["previous_review"] = state.get("review_report")
        budget = await self._context_budget("reviewer", reviewer)
        input_data = self._build_reviewer_input(state, budget)

        try:
            result = await self._call_agent(
                reviewer, input_data, "reviewer", cost_tracker, state, store=store
            )
        except WallTimeExceeded:
            raise
        except Exception as e:
            self._agent_failed(state, store, "reviewer", e)
            return state

        (state.get("_agent_failures") or {}).pop("reviewer", None)
        state["review_report"] = result

        # Count issues by severity
        issues = result.get("issues", [])
        severity_counts: dict[str, int] = {}
        for issue in issues:
            sev = issue.get("severity", "unknown")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        state["messages"].append(
            {
                "event": "reviewer_complete",
                "acceptance_met": result.get("acceptance_met", False),
                "issue_count": len(issues),
                "severity_counts": severity_counts,
                "cost_usd": cost_tracker.cumulative_cost_usd,
            }
        )

        logger.info(
            "Review: %s (%d issues: %s)",
            "APPROVED" if result.get("acceptance_met") else "CHANGES REQUIRED",
            len(issues),
            severity_counts,
        )
        return state

    def _build_reviewer_input(
        self, state: AgentState, budget: int = DEFAULT_FILE_BUDGET
    ) -> dict[str, Any]:
        """Reviewer input: the current task, what this attempt changed, and the evidence.

        The diff is synthesized from the pre-patch snapshot (implementers return
        whole files; a patch's own ``diff_unified`` is shown when nothing was
        changed).  Later tasks are listed so they are not demanded now, and only
        the previous review of the same task is shown (``previous_review`` is
        reset when the task changes).  Known failures are shown apart, with
        their origin, and left out of the test report's failure excerpt.
        Related files get *budget* bytes.
        """
        root = Path(state["workspace_root"])
        patch = state.get("current_patch") or {}
        changed: list[str] = state.get("_last_changed_files") or []
        after = _read_workspace_files(root, changed)
        if changed:
            before = state.get("_pre_patch_snapshot") or {}
            diff = synthesize_diff({path: before.get(path) for path in changed}, after)
        else:
            diff = str(patch.get("diff_unified") or "")
        task = state.get("current_task") or {}
        finished = _finished_task_ids(state) | {task.get("task_id")}
        report = state.get("test_report")
        baseline = (report or {}).get("baseline_failures") or []
        if report and baseline:
            report = _without_known_failures(report)

        input_data: dict[str, Any] = {
            "request": state["request"],
            "task": task,
            "iteration": state["iteration"],
            "task_attempt": state.get("_task_iteration", 0) + 1,
            "diff": diff,
            "patch_noop": bool(state.get("patch_noop")),
            "test_report": report,
        }
        evidence: dict[str, Any] = {
            "acceptance": (state.get("plan") or {}).get("acceptance"),
            "later_tasks": [
                t.get("title", "")
                for t in state.get("all_tasks") or []
                if t.get("task_id") not in finished
            ],
            "changed_files": {path: text for path, text in after.items() if text is not None},
            "related_files": self._review_related_files(state, changed, budget),
            "previous_issues": (state.get("previous_review") or {}).get("issues"),
            "import_warnings": state.get("import_issues"),
            "removed_tests": state.get("_removed_tests"),
            "baseline_failures": _known_failure_origins(state, baseline),
            "security_findings": state.get("security_findings"),
            "constraints": self._constraints_input(),
        }
        input_data.update({key: value for key, value in evidence.items() if value})
        return input_data

    def _review_related_files(
        self, state: AgentState, changed: Collection[str], budget: int = DEFAULT_FILE_BUDGET
    ) -> dict[str, str]:
        """Unchanged files the reviewer needs: those of the failing tests and what they import.

        For an attempt that changed nothing (the reviewer sees no diff then),
        also the files it resubmitted and the files the task wrote earlier.
        At most *budget* bytes.
        """
        report = state.get("test_report")
        failing = (
            report
            if report and classify_test_evidence(report) == "failed" and failing_test_ids(report)
            else None
        )
        unchanged: list[str] = []
        if state.get("patch_noop"):
            resubmitted = (state.get("current_patch") or {}).get("files") or {}
            unchanged = list(dict.fromkeys([*resubmitted, *_task_files(state)]))
        if failing is None and not unchanged:
            return {}
        files = gather_context(
            Path(state["workspace_root"]),
            file_tree=state.get("file_tree", []),
            max_total_size=budget,
            prior_files=unchanged,
            test_report=failing,
        )
        return {path: text for path, text in files.items() if path not in changed}

    def _estimate_plan_cost(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Estimate cost based on plan task count and configured models."""
        from delegatron.utils.cost import CostTracker

        tracker = CostTracker()
        task_count = max(1, len(plan.get("tasks", [])))
        max_iter = self.config.pipeline.max_iterations

        # Architect: already called once (actual cost known from state)
        arch_model = self.config.architect.model
        arch_cost = tracker.estimate_cost(arch_model, 2000, 1500)

        # Per-iteration: implementer + reviewer
        impl_model = self.config.implementer.model
        rev_model = self.config.reviewer.model
        impl_cost = tracker.estimate_cost(impl_model, 3000, 2000)
        rev_cost = tracker.estimate_cost(rev_model, 4000, 1000)
        iter_cost = impl_cost + rev_cost

        return {
            "architect_cost": round(arch_cost, 4),
            "per_iteration_cost": round(iter_cost, 4),
            "estimated_min": round(arch_cost + iter_cost, 4),
            "estimated_max": round(arch_cost + (iter_cost * max_iter), 4),
            "task_count": task_count,
            "max_iterations": max_iter,
            "models": {
                "architect": arch_model,
                "implementer": impl_model,
                "reviewer": rev_model,
            },
        }

    async def _interactive_approve_plan(self, plan: dict[str, Any]) -> bool:
        """Pause for user approval of an architect plan; return whether it was approved.

        Uses ``approval_callback`` when provided.  The callback receives the
        plan dict (enriched with cost estimate) and returns ``True`` to approve.
        When no callback is set (e.g. server context) the plan is auto-approved
        with a warning so that ``input()`` is never called outside a terminal.

        The callback may be sync or async — both are handled.
        """
        import inspect

        # Enrich plan with cost estimate
        plan["cost_estimate"] = self._estimate_plan_cost(plan)

        if self.approval_callback is not None:
            result = self.approval_callback(plan)
            if inspect.isawaitable(result):
                approved = await result
            else:
                approved = result
        else:
            # No callback — only use input() when stdin is a TTY
            import sys

            if sys.stdin is not None and sys.stdin.isatty():
                approved = self._cli_approve_plan(plan)
            else:
                logger.warning(
                    "interactive=True but no approval_callback and stdin is not a TTY; "
                    "auto-approving plan"
                )
                approved = True
        return bool(approved)

    @staticmethod
    def _cli_approve_plan(plan: dict[str, Any]) -> bool:
        """Prompt the user for plan approval on the terminal."""
        summary = plan.get("summary", "No summary")
        tasks = plan.get("tasks", [])

        print(f"\n{'=' * 60}")
        print(f"PLAN: {summary}")
        print(f"Tasks: {len(tasks)}")
        for t in tasks:
            print(f"  [{t.get('risk_level', '?')}] {t.get('task_id')}: {t.get('title')}")
        print(f"{'=' * 60}")

        try:
            response = input("\nApprove this plan? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            response = "n"

        return response in ("y", "yes")

    def _apply_to_workspace(
        self,
        state: AgentState,
        workspace: Workspace,
        *,
        is_git_repo: bool,
        store: ArtifactStore | None = None,
    ) -> None:
        """Apply the current patch, record what it changed, and commit in a git repo.

        Before writing, the patched paths' content goes to ``_pre_patch_snapshot``;
        afterwards ``_last_changed_files``/``patch_noop`` say which paths really
        changed (ignoring CRLF vs LF) and ``_file_history`` records the task and
        iteration that last changed each one.  ``file_tree`` is refreshed after
        every write attempt.  Unsafe paths are skipped and listed in ``_rejected_paths``.
        File writes work without git; diff-only patches and commits require it.
        A failed commit leaves the patch applied (``commit_failed`` event).
        """
        patch = state.get("current_patch") or {}
        files: dict[str, str] = patch.get("files") or {}
        deletes: list[str] = patch.get("delete_files") or []
        diff: str = patch.get("diff_unified") or ""
        state["_rejected_paths"] = []
        state["_last_changed_files"] = []
        state["_pre_patch_snapshot"] = {}
        state["patch_noop"] = False

        if not files and not deletes and not diff:
            # No changes in the response: keep the task's applied state unless
            # nothing of this task has been tested yet.
            state["patch_noop"] = True
            if state.get("test_report") is None:
                state["patch_applied"] = False
            return
        if not files and diff and not is_git_repo:
            logger.warning("Diff patch but workspace is not a git repo; skipping apply")
            state["patch_applied"] = False
            return

        touched: list[str] | None = None
        error = "the diff did not apply cleanly"
        try:
            touched = self._write_patch(state, workspace, files, deletes, diff)
        except Exception as e:
            error = str(e)
            logger.warning("Could not apply patch to workspace: %s", e)
        if touched is None:
            _emit_event(
                state,
                store,
                {"event": "patch_apply_failed", "iteration": state["iteration"], "error": error},
            )
        if state["_rejected_paths"]:
            _emit_event(
                state,
                store,
                {
                    "event": "paths_rejected",
                    "iteration": state["iteration"],
                    "paths": state["_rejected_paths"],
                },
            )
        # Applied unless it failed or every path was rejected (deleting an
        # already-missing file is an applied no-op).
        state["patch_applied"] = touched is not None and bool(
            touched or not state["_rejected_paths"]
        )
        try:
            if touched is not None and state["patch_applied"]:
                changed = self._record_changes(state, workspace, touched)
                if is_git_repo:
                    self._commit_workspace(state, workspace, patch, changed, store)
        finally:
            state["file_tree"] = workspace.get_file_tree()

    @staticmethod
    def _write_patch(
        state: AgentState,
        workspace: Workspace,
        files: dict[str, str],
        deletes: list[str],
        diff: str,
    ) -> list[str] | None:
        """Snapshot, then write the patch; return the paths written or deleted.

        Whole ``files`` win over a diff, which is then ignored.  Without files,
        the diff is applied, then the deletions.  Returns None when the diff
        does not apply cleanly (nothing is written then).
        """
        if files or not diff:
            state["_pre_patch_snapshot"] = workspace.snapshot_files([*files, *deletes])
            result = workspace.apply_files(files, deletes)
            state["_rejected_paths"] = result.rejected
            return [*result.written, *result.deleted]
        paths = workspace.diff_paths(diff)
        state["_pre_patch_snapshot"] = workspace.snapshot_files([*paths, *deletes])
        if not workspace.apply_patch(diff):
            logger.warning("Patch did not apply cleanly to workspace")
            return None
        if not deletes:
            return paths
        result = workspace.apply_files({}, deletes)
        state["_rejected_paths"] = result.rejected
        return list(dict.fromkeys([*paths, *result.deleted]))

    @staticmethod
    def _record_changes(state: AgentState, workspace: Workspace, touched: list[str]) -> list[str]:
        """Record which touched paths really changed, and the task that changed them.

        A path either snapshot could not read (the snapshot leaves it out)
        counts as changed: the write may well have changed it.
        """
        before = state["_pre_patch_snapshot"]
        after = workspace.snapshot_files(touched)
        changed = sorted(
            {
                path
                for path in touched
                if path not in before or path not in after or before[path] != after[path]
            }
        )
        task_id = (state.get("current_task") or {}).get("task_id", "")
        history = state.setdefault("_file_history", {})
        for path in changed:
            entry: dict[str, Any] = {"task_id": task_id, "iteration": state["iteration"]}
            if path in after and after[path] is None:
                entry["deleted"] = True
            history[path] = entry
        state["_pre_patch_snapshot"] = {path: before.get(path) for path in changed}
        state["_last_changed_files"] = changed
        state["patch_noop"] = not changed
        return changed

    @staticmethod
    def _commit_workspace(
        state: AgentState,
        workspace: Workspace,
        patch: dict[str, Any],
        changed: list[str],
        store: ArtifactStore | None,
    ) -> None:
        """Commit the applied paths on the run branch; a failure keeps the patch applied."""
        rationale = " ".join(str(patch.get("rationale") or "code changes").split())[:80]
        message = f"{RUN_COMMIT_PREFIX} {state['iteration']} — {rationale}"
        Pipeline._commit(state, workspace, message, store, changed)

    @staticmethod
    def _deleted_files(state: AgentState, paths: Iterable[str]) -> list[str]:
        """Those of *paths* the run deleted, according to ``_file_history``."""
        history = state.get("_file_history") or {}
        return [path for path in paths if (history.get(path) or {}).get("deleted")]

    @staticmethod
    def _commit(
        state: AgentState,
        workspace: Workspace,
        message: str,
        store: _EventSink | None,
        changed: list[str] | None = None,
    ) -> bool:
        """Commit every path written since the last commit; return whether that succeeded.

        A failure is recorded (``workspace_commit_error``, ``commit_failed``)
        with git's own error, and the paths stay pending for the next commit.
        ``workspace_commit`` lists the *changed* paths it was asked to commit
        and, apart, those of them the patch deleted (``files_deleted``).
        """
        try:
            sha = workspace.commit(message)
        except Exception as e:
            error = _commit_error_text(e)
            state["workspace_commit_error"] = error
            logger.warning("Workspace commit failed (the patch stays applied): %s", error)
            _emit_event(
                state,
                store,
                {"event": "commit_failed", "iteration": state["iteration"], "error": error},
            )
            return False
        state["workspace_commit_error"] = ""
        if sha is None:
            logger.info("Nothing to commit")
            return True
        state["workspace_sha"] = sha
        logger.info("Committed to workspace: %s", sha[:8])
        event: dict[str, Any] = {
            "event": "workspace_commit",
            "sha": sha,
            "iteration": state["iteration"],
        }
        if changed is not None:
            event["changed_files"] = changed
            event["files_deleted"] = Pipeline._deleted_files(state, changed)
        _emit_event(state, store, event)
        return True

    def _run_import_validation(self, state: AgentState, store: ArtifactStore | None) -> None:
        """Check the applied patch's imports (all languages) against the workspace.

        Runs after apply, so modules created by the same patch resolve.  The
        findings are advisory and never gate acceptance; ``import_issues`` is
        replaced on every iteration and cleared when the task changes.
        """
        patch = state.get("current_patch")
        issues = validate_patch_imports(patch, self.workspace_root) if patch else []
        state["import_issues"] = [
            {
                "severity": iss.severity,
                "title": f"Unresolvable import: {iss.module}",
                "details": iss.message,
                "file": iss.file,
                "evidence": iss.file,
                "source": "import_validator",
            }
            for iss in issues
        ]
        for issue in state["import_issues"]:
            _emit_event(state, store, {"event": "import_issue", **issue})
        if issues:
            logger.warning("Import validator found %d unresolvable import(s) in patch", len(issues))

    def _maybe_create_pr(
        self,
        state: AgentState,
        workspace: Workspace,
        summary: dict[str, Any],
        is_git_repo: bool,
    ) -> dict[str, Any]:
        """Open a GitHub PR if the run was approved and `create_pr` is enabled.

        Returns a dict with ``created`` (bool), plus either ``url`` + ``number``
        on success or ``error`` + ``skipped_reason`` on failure. Never raises:
        any failure on the way (``gh`` hanging or missing, a malformed remote
        URL) is logged and recorded as ``error``, so the approved run still
        gets its summary.
        """
        try:
            return self._create_pr(state, workspace, summary, is_git_repo)
        except Exception as e:
            logger.warning("PR creation failed: %s", e, exc_info=True)
            return {"created": False, "error": f"{type(e).__name__}: {e}"}

    def _create_pr(
        self,
        state: AgentState,
        workspace: Workspace,
        summary: dict[str, Any],
        is_git_repo: bool,
    ) -> dict[str, Any]:
        """``_maybe_create_pr`` without the guard: may raise."""
        result: dict[str, Any] = {"created": False}

        if not self.config.pipeline.create_pr:
            return result
        if not state.get("is_approved") or state.get("status") != "approved":
            result["skipped_reason"] = f"run status is {state.get('status')}, not approved"
            return result
        if not is_git_repo:
            result["skipped_reason"] = "workspace is not a git repo"
            return result
        if not workspace.branch:
            result["skipped_reason"] = "run branch was never initialized"
            return result
        if state.get("workspace_commit_error"):
            # The branch would lack approved code that was never committed.
            result["skipped_reason"] = (
                f"last workspace commit failed: {state.get('workspace_commit_error')}"
            )
            return result
        if state.get("_security_blockers") or (summary.get("security") or {}).get(
            "unresolved_blocking"
        ):
            result["skipped_reason"] = "unresolved blocking security findings"
            return result

        ok, reason = check_gh_available()
        if not ok:
            logger.warning("PR skipped: %s", reason)
            result["skipped_reason"] = reason
            return result

        ok, reason = check_git_remote(workspace.repo_path)
        if not ok:
            logger.warning("PR skipped: %s", reason)
            result["skipped_reason"] = reason
            return result

        title = (state.get("plan") or {}).get("summary") or "Delegatron: code changes"
        title = title[:72]
        body = build_pr_body(dict(state), summary)

        creator = self.pr_creator or GitHubPRCreator()
        # The push sends the run branch to the remote's host; ``gh pr create``
        # then calls the GitHub API (``GH_HOST`` for GitHub Enterprise).
        self._net_guard.record_external_call(
            "git_push", remote_host(git_remote_url(workspace.repo_path))
        )
        self._net_guard.record_external_call(
            "vcs_api", os.environ.get("GH_HOST", "").strip() or "github.com"
        )
        try:
            pr = creator.create_pr(
                workspace=workspace.repo_path,
                branch=workspace.branch,
                title=title,
                body=body,
                base=self.config.pipeline.pr_base,
                draft=self.config.pipeline.pr_draft,
            )
        except Exception as e:
            logger.warning("PR creation raised: %s", e)
            result["error"] = f"{type(e).__name__}: {e}"
            return result

        if pr.success:
            logger.info("PR created: %s", pr.url)
            result["created"] = True
            result["url"] = pr.url
            result["number"] = pr.number
            state["messages"].append({"event": "pr_created", "url": pr.url, "number": pr.number})
        else:
            logger.warning("PR creation failed: %s", pr.error)
            result["error"] = pr.error

        return result

    def _route(self, state: AgentState, store: ArtifactStore) -> None:
        """Route the next step after a review that did not finish the task.

        Approved (the last task), a re-plan (``_replan``; an architecture issue
        past the re-plan limit emits ``replan_suppressed`` and goes to the
        implementer), or another implementer pass.
        """
        direction = route_after_review(dict(state), max_replans=self.config.pipeline.max_replans)

        if direction == "approved":
            state["status"] = "approved"
        elif direction == "exhausted":
            state["status"] = "exhausted"
        elif direction == "architect":
            # Signal the main loop to re-run the architect before next iteration
            state["_replan"] = True
        elif direction == "implementer":
            if architecture_issues(state.get("review_report")) or state.get("replan_request"):
                _emit_event(
                    state,
                    store,
                    {
                        "event": "replan_suppressed",
                        "iteration": state["iteration"],
                        "replans": state.get("_replan_count", 0),
                    },
                )
                state["replan_request"] = None
        else:
            state["status"] = "error"
            state["error"] = f"Unexpected route direction: {direction}"
            logger.error("Invalid router direction: %s", direction)

        state["messages"].append(
            {
                "event": "routed",
                "direction": direction,
                "iteration": state["iteration"],
            }
        )
