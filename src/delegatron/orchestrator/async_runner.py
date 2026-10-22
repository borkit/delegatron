"""Legacy event-driven pipeline runner (deprecated).

``AsyncPipeline`` is a separate, older implementation of the plan → code →
test → review loop with subscriber fan-out, cooperative cancellation and
status tracking. It does not wrap :class:`delegatron.orchestrator.graph.Pipeline`
and no longer receives its fixes.  Known gaps compared to ``graph.Pipeline``:

- only the first planned task is worked on;
- only ``diff_unified`` patches are applied to the workspace (``files``
  patches reach the sandbox only);
- ``security_mode`` is ignored: security findings are emitted as events and
  the patch is applied anyway (``"block"`` blocks nothing);
- no import validation, no two-pass implementer, no resume and no
  zero-cloud report;
- arbitration conflicts are only logged.

Use ``graph.Pipeline`` instead: its ``step_callback`` streams progress events
(the API server builds SSE on it) and cancelling the task that awaits
``execute()`` stops the run. This module is kept for backward compatibility;
constructing ``AsyncPipeline`` emits a ``DeprecationWarning``.

Usage::

    async_pipeline = AsyncPipeline(config=config, workspace_root=root, run_id=rid)

    # Subscribe before executing
    async def log_events():
        async for event in async_pipeline.subscribe():
            print(event)

    asyncio.create_task(log_events())
    state = await async_pipeline.execute("add login endpoint")
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import logging
import time
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from delegatron.agents.architect import ArchitectAgent
from delegatron.agents.implementer import ImplementerAgent
from delegatron.agents.reviewer import ReviewerAgent
from delegatron.artifacts import ArtifactStore
from delegatron.config import Config, RoleConfig, is_local_model
from delegatron.constraints import Constraints
from delegatron.context import gather_context, gather_repo_map
from delegatron.detection import detect_project
from delegatron.factories.agent_factory import create_architect, create_implementer, create_reviewer
from delegatron.factories.sandbox_factory import create_sandbox, get_install_commands
from delegatron.factories.security_pipeline import SecurityBaseline, run_security_checks
from delegatron.orchestrator.arbitration import detect_conflict
from delegatron.orchestrator.scoring import (
    can_override_rejection,
    get_score_summary,
    should_accept,
)
from delegatron.orchestrator.state import AgentState
from delegatron.orchestrator.termination import detect_stall, should_terminate
from delegatron.sandbox.base import BaseSandbox
from delegatron.sandbox.toolchains import PYTEST_COMMAND
from delegatron.utils.cost import CostTracker, get_gpu_power_watts
from delegatron.utils.provider import is_local_base_url
from delegatron.workspace import RUN_COMMIT_PREFIX, Workspace, WorkspaceInitError

logger = logging.getLogger(__name__)


class _EarlyExit(Exception):
    """Internal sentinel to break out of the pipeline while still saving a summary."""


class PipelineStatus(enum.Enum):
    """Current status of an AsyncPipeline."""

    IDLE = "idle"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PipelineEvent:
    """An event emitted during pipeline execution.

    Attributes:
        event_type: One of plan_created, iteration_start, patch_generated,
            tests_complete, review_complete, constraint_violation,
            secrets_detected, approved, exhausted, error, cancelled.
        iteration: The pipeline iteration when this event occurred.
        timestamp: UTC timestamp of the event.
        data: Event-specific payload dictionary.
    """

    event_type: str
    iteration: int
    timestamp: datetime
    data: dict[str, Any]


@dataclass
class AsyncPipeline:
    """Deprecated event-driven pipeline; use ``graph.Pipeline`` instead.

    Provides async event streaming, multiple concurrent subscribers,
    and cooperative cancellation between pipeline steps. See the module
    docstring for what it lacks compared to ``graph.Pipeline``.
    """

    config: Config
    workspace_root: Path
    run_id: str
    artifacts_dir: Path | None = None
    # Start a run even when the git workspace has uncommitted changes.
    allow_dirty: bool = False

    _status: PipelineStatus = field(default=PipelineStatus.IDLE, init=False)
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _subscribers: list[asyncio.Queue[PipelineEvent | None]] = field(
        default_factory=list, init=False
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        warnings.warn(
            "AsyncPipeline is deprecated; use delegatron.orchestrator.graph.Pipeline",
            DeprecationWarning,
            stacklevel=3,
        )

    def status(self) -> PipelineStatus:
        """Return the current pipeline status."""
        return self._status

    def cancel(self) -> None:
        """Request cancellation of the running pipeline.

        The pipeline checks this flag between major steps and will
        stop at the next safe point.
        """
        self._cancel_event.set()

    async def subscribe(self) -> AsyncIterator[PipelineEvent]:
        """Async generator that yields events as they are emitted.

        Multiple callers can subscribe concurrently; each receives
        all events (fan-out). The generator terminates when the
        pipeline completes, fails, or is cancelled.
        """
        queue: asyncio.Queue[PipelineEvent | None] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            async with self._lock:
                if queue in self._subscribers:
                    self._subscribers.remove(queue)

    async def execute(self, request: str) -> AgentState:
        """Run the full pipeline, emitting events to all subscribers.

        Returns the final AgentState, same as Pipeline.execute.
        """
        self._status = PipelineStatus.RUNNING
        self._cancel_event.clear()

        cost_tracker = CostTracker()
        start_time = time.monotonic()

        # Initialize artifact store
        artifacts_base = self.artifacts_dir or (self.workspace_root / "runs")
        store = ArtifactStore(base_dir=artifacts_base, run_id=self.run_id)
        cost_tracker.set_log_file(store.run_dir / "provider_calls.jsonl")
        store.append_event({"event": "pipeline_start", "request": request, "run_id": self.run_id})

        # Detect project and set up workspace
        project_info = detect_project(self.workspace_root)
        workspace = Workspace(repo_path=self.workspace_root, run_id=self.run_id)
        # A subdirectory of a repository is a git workspace too.
        is_git_repo = Workspace.find_repo_root(self.workspace_root) is not None
        workspace_error = self._init_workspace(workspace, store) if is_git_repo else ""

        state: AgentState = {
            "messages": [],
            "request": request,
            "project_info": {
                "languages": project_info.languages,
                "frameworks": project_info.frameworks,
                "test_commands": project_info.test_commands,
                "lint_commands": project_info.lint_commands,
                "package_manager": project_info.package_manager,
            },
            "file_tree": workspace.get_file_tree(),
            "workspace_root": str(self.workspace_root),
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
            "_task_iteration": 0,
            "all_tasks": [],
            "execution_waves": [],
            "_replan": False,
        }

        sandbox: BaseSandbox | None = None
        try:
            # Fail closed: without the run branch, commits would land on the
            # user's current branch.
            if workspace_error:
                state["status"] = "error"
                state["error"] = workspace_error
                self._status = PipelineStatus.FAILED
                await self._emit(
                    PipelineEvent(
                        event_type="error",
                        iteration=0,
                        timestamp=datetime.now(UTC),
                        data={"error": workspace_error},
                    )
                )
                raise _EarlyExit

            # Create agents
            architect = create_architect(self.config)
            implementer = create_implementer(self.config)
            reviewer = create_reviewer(self.config)

            # Create sandbox based on config
            sandbox = create_sandbox(
                sandbox_type=self.config.pipeline.sandbox,
                workspace_root=self.workspace_root,
                test_commands=project_info.test_commands or [PYTEST_COMMAND],
                install_commands=get_install_commands(project_info, self.workspace_root),
                docker_image=self.config.pipeline.docker_image,
                primary_language=(project_info.languages[0] if project_info.languages else ""),
            )

            constraints_data = self.config.pipeline.constraints
            constraints = Constraints.from_dict(constraints_data) if constraints_data else None
            # One per run: a finding stays reported until the line is gone,
            # whether or not a later patch touches the file again.
            security_baseline = SecurityBaseline(self.workspace_root)
            # Check cancellation before architect
            if self._is_cancelled():
                self._handle_cancel(state)
                raise _EarlyExit

            # Step 1: Architect
            state = await self._run_architect(state, architect, cost_tracker)
            if state["status"] == "error":
                self._status = PipelineStatus.FAILED
                state["is_approved"] = False
                await self._emit(
                    PipelineEvent(
                        event_type="error",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={"error": state["error"]},
                    )
                )
                raise _EarlyExit

            await self._emit(
                PipelineEvent(
                    event_type="plan_created",
                    iteration=state["iteration"],
                    timestamp=datetime.now(UTC),
                    data={
                        "summary": (state.get("plan") or {}).get("summary", ""),
                        "task_count": len((state.get("plan") or {}).get("tasks", [])),
                    },
                )
            )

            # Save plan artifact
            plan_snapshot = state.get("plan")
            if plan_snapshot:
                store.save_plan(plan_snapshot)

            # Main loop
            while state["status"] == "running":
                # Check termination BEFORE incrementing — ensures
                # max_iterations=N allows exactly N complete cycles,
                # matching graph.py behavior.
                if should_terminate(
                    dict(state),
                    max_iterations=self.config.pipeline.max_iterations,
                    max_cost_usd=self.config.pipeline.max_cost_usd,
                ):
                    state["status"] = "exhausted"
                    await self._emit(
                        PipelineEvent(
                            event_type="exhausted",
                            iteration=state["iteration"],
                            timestamp=datetime.now(UTC),
                            data={"reason": "Budget or iteration limit reached"},
                        )
                    )
                    break

                # Check wall time
                elapsed_minutes = (time.monotonic() - start_time) / 60
                if elapsed_minutes >= self.config.pipeline.max_wall_time_minutes:
                    state["status"] = "exhausted"
                    await self._emit(
                        PipelineEvent(
                            event_type="exhausted",
                            iteration=state["iteration"],
                            timestamp=datetime.now(UTC),
                            data={"reason": "Wall time limit reached"},
                        )
                    )
                    break

                # Re-run architect if the router requested re-planning
                if cast(dict[str, Any], state).pop("_replan", False):
                    state = await self._run_architect(
                        state,
                        architect,
                        cost_tracker,
                    )
                    if state["status"] == "error":
                        break

                state["iteration"] += 1

                # Check cancellation before implementer
                if self._is_cancelled():
                    self._handle_cancel(state)
                    break

                await self._emit(
                    PipelineEvent(
                        event_type="iteration_start",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={"iteration": state["iteration"]},
                    )
                )

                # Implementer
                state = await self._run_implementer(state, implementer, cost_tracker)
                if state["status"] == "error":
                    self._status = PipelineStatus.FAILED
                    await self._emit(
                        PipelineEvent(
                            event_type="error",
                            iteration=state["iteration"],
                            timestamp=datetime.now(UTC),
                            data={"error": state["error"]},
                        )
                    )
                    break

                await self._emit(
                    PipelineEvent(
                        event_type="patch_generated",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={
                            "status": (state.get("current_patch") or {}).get("status", "ok"),
                            "rationale": (state.get("current_patch") or {}).get("rationale", ""),
                        },
                    )
                )

                # Save patch artifact
                patch_snapshot = state.get("current_patch")
                if patch_snapshot:
                    store.save_patch(state["iteration"], patch_snapshot)

                # Security checks (constraints, secrets, deps)
                if patch_snapshot:
                    security_result = run_security_checks(
                        patch_snapshot,
                        constraints,
                        self.workspace_root,
                        secrets_allowlist=self.config.security.secrets_allowlist,
                        baseline=security_baseline,
                    )
                    if security_result.has_blockers:
                        for evt in security_result.summary_events:
                            await self._emit(
                                PipelineEvent(
                                    event_type=evt["event"],
                                    iteration=state["iteration"],
                                    timestamp=datetime.now(UTC),
                                    data=evt,
                                )
                            )

                # Apply patch to workspace
                if is_git_repo:
                    self._apply_to_workspace(state, workspace)

                # Check cancellation before sandbox
                if self._is_cancelled():
                    self._handle_cancel(state)
                    break

                # Sandbox tests
                state = await self._run_sandbox(state, sandbox, applied_diff=is_git_repo)

                await self._emit(
                    PipelineEvent(
                        event_type="tests_complete",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={
                            "passed": (state.get("test_report") or {}).get("passed", False),
                            "total": (state.get("test_report") or {}).get("total", 0),
                            "failures": (state.get("test_report") or {}).get("failures", 0),
                        },
                    )
                )

                # Save test report
                test_snapshot = state.get("test_report")
                if test_snapshot:
                    store.save_test_report(state["iteration"], test_snapshot)

                # Check cancellation before reviewer
                if self._is_cancelled():
                    self._handle_cancel(state)
                    break

                # Reviewer
                state = await self._run_reviewer(state, reviewer, cost_tracker)
                if state["status"] == "error":
                    self._status = PipelineStatus.FAILED
                    await self._emit(
                        PipelineEvent(
                            event_type="error",
                            iteration=state["iteration"],
                            timestamp=datetime.now(UTC),
                            data={"error": state["error"]},
                        )
                    )
                    break

                await self._emit(
                    PipelineEvent(
                        event_type="review_complete",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={
                            "reviewer_accepted": (state.get("review_report") or {}).get(
                                "acceptance_met", False
                            ),
                            "accepted": state.get("is_approved", False),
                            "issue_count": len(
                                (state.get("review_report") or {}).get("issues", [])
                            ),
                        },
                    )
                )

                # Save review
                review_snapshot = state.get("review_report")
                if review_snapshot:
                    store.save_review(state["iteration"], review_snapshot)

                # EMCS scoring: it may approve a rejection whose issues are all
                # minor, given real passing tests; the review itself is kept.
                emcs_review = state.get("review_report")
                if emcs_review:
                    threshold = self.config.pipeline.emcs_threshold
                    applied = state.get("patch_applied", False)
                    score_summary = get_score_summary(
                        emcs_review,
                        state.get("test_report"),
                        patch_applied=applied,
                        threshold=threshold,
                    )
                    state["messages"].append({"event": "emcs_score", **score_summary})
                    store.append_event({"event": "emcs_score", **score_summary})
                    if not state.get("is_approved") and can_override_rejection(
                        emcs_review,
                        state.get("test_report"),
                        threshold=threshold,
                        patch_applied=applied,
                    ):
                        state["is_approved"] = True
                        override = {
                            "event": "emcs_override",
                            "iteration": state["iteration"],
                            "score": score_summary.get("score"),
                            "threshold": threshold,
                            "issue_counts": score_summary.get("issue_counts", {}),
                        }
                        state["messages"].append(override)
                        store.append_event(override)

                # Arbitration
                if (
                    self.config.pipeline.enable_arbitration
                    and state.get("review_report")
                    and not state.get("is_approved")
                ):
                    review_for_conflict = state["review_report"]
                    assert review_for_conflict is not None
                    conflicts = detect_conflict(
                        review_for_conflict,
                        state.get("previous_review"),
                        state["iteration"],
                    )
                    if conflicts:
                        logger.info("Arbitration triggered for %d conflicts", len(conflicts))

                # Save state checkpoint
                store.save_state(dict(state))

                # Route
                state = self._route(state)

                if state["status"] == "approved":
                    await self._emit(
                        PipelineEvent(
                            event_type="approved",
                            iteration=state["iteration"],
                            timestamp=datetime.now(UTC),
                            data={"is_approved": True},
                        )
                    )

            # Emit cancelled event after breaking out of the loop
            if state["status"] == "cancelled":
                await self._emit(
                    PipelineEvent(
                        event_type="cancelled",
                        iteration=state["iteration"],
                        timestamp=datetime.now(UTC),
                        data={"reason": "Cancellation requested"},
                    )
                )

        except _EarlyExit:
            pass  # State already set; fall through to summary

        except Exception as e:
            logger.exception("Pipeline error")
            state["status"] = "error"
            state["is_approved"] = False
            state["error"] = str(e)
            self._status = PipelineStatus.FAILED
            await self._emit(
                PipelineEvent(
                    event_type="error",
                    iteration=state["iteration"],
                    timestamp=datetime.now(UTC),
                    data={"error": str(e)},
                )
            )
        finally:
            if sandbox is not None:
                try:
                    sandbox.cleanup()
                except Exception:
                    logger.warning("Sandbox cleanup failed", exc_info=True)

        # Final summary
        elapsed = time.monotonic() - start_time
        summary = {
            "run_id": self.run_id,
            "status": state["status"],
            "iterations": state["iteration"],
            "total_cost_usd": state["total_cost_usd"],
            "elapsed_seconds": round(elapsed, 1),
            "request": request,
            "plan_summary": (state.get("plan") or {}).get("summary", ""),
            "is_approved": state.get("is_approved", False),
            "error": state.get("error", ""),
            "cost_breakdown": cost_tracker.get_summary(),
        }
        try:
            store.save_summary(summary)
        except Exception as e:
            logger.critical("Failed to save summary for run %s: %s", self.run_id, e)
            try:
                (store.run_dir / "summary.json").write_text(
                    json.dumps(summary, indent=2, default=str)
                )
            except Exception:
                logger.critical("Fallback summary write also failed for run %s", self.run_id)

        if self._status == PipelineStatus.RUNNING:
            self._status = PipelineStatus.COMPLETED

        # Unload models from GPU
        try:
            from delegatron.utils.gpu_cleanup import cleanup_after_run

            cleanup_after_run(self.config)
        except Exception:
            logger.debug("GPU cleanup failed (non-fatal)")

        await self._close_subscribers()
        return state

    def _init_workspace(self, workspace: Workspace, store: ArtifactStore) -> str:
        """Check out a fresh run branch; return why it failed ("" on success)."""
        try:
            info = workspace.initialize(allow_dirty=self.allow_dirty, fresh=True)
        except WorkspaceInitError as e:
            logger.error("Workspace setup failed: %s", e)
            store.append_event({"event": "workspace_init_failed", "error": str(e)})
            return f"Workspace setup failed: {e}"
        logger.info("Workspace initialized: branch=%s, commit=%s", info.branch, info.commit_sha[:8])
        store.append_event(
            {
                "event": "workspace_initialized",
                "branch": info.branch,
                "base_branch": info.base_branch,
            }
        )
        return ""

    def _record_call(
        self,
        cost_tracker: CostTracker,
        state: AgentState,
        role: str,
        role_config: RoleConfig,
        meta: dict[str, Any],
        *,
        generation_seconds: float,
        gpu_watts: float | None,
    ) -> None:
        """Book one agent call; whether it ran locally comes from the role's endpoint."""
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
            is_local=is_local_model(role_config),
            cache_read_tokens=meta.get("cache_read_tokens", 0),
            cache_write_tokens=meta.get("cache_write_tokens", 0),
            temperature=meta.get("temperature"),
        )
        state["total_cost_usd"] = cost_tracker.total_cost_usd

    async def _invoke(
        self,
        agent: ArchitectAgent | ImplementerAgent | ReviewerAgent,
        input_data: dict[str, Any],
        role: str,
        role_config: RoleConfig,
        cost_tracker: CostTracker,
        state: AgentState,
    ) -> dict[str, Any]:
        """Invoke *agent* and book the call; the GPU power of a local role is
        sampled right after its generation.  Returns the artifact without ``_meta``."""
        start = time.monotonic()
        result = await agent.invoke(input_data)
        generation_seconds = time.monotonic() - start
        gpu_watts = (
            await asyncio.to_thread(get_gpu_power_watts) if is_local_model(role_config) else None
        )
        meta = result.pop("_meta", {}) or {}
        self._record_call(
            cost_tracker,
            state,
            role,
            role_config,
            meta,
            generation_seconds=generation_seconds,
            gpu_watts=gpu_watts,
        )
        return result

    # --- Event helpers ---

    def _is_cancelled(self) -> bool:
        """Check whether cancellation has been requested."""
        return self._cancel_event.is_set()

    def _handle_cancel(self, state: AgentState) -> AgentState:
        """Apply cancellation to the state.

        Sets status fields so the summary block at the end of execute()
        persists a proper cancelled summary.
        """
        state["status"] = "cancelled"
        state["is_approved"] = False
        self._status = PipelineStatus.CANCELLED
        return state

    async def _emit(self, event: PipelineEvent) -> None:
        """Push an event to all subscriber queues (non-blocking fan-out).

        Uses put_nowait with drop-oldest semantics so a slow/stalled
        subscriber cannot backpressure the pipeline. If a queue is full,
        the oldest event is discarded and the new event is enqueued.
        """
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest event to make room — subscriber is too slow
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning("Subscriber queue still full after drop — event lost")
        # Yield to allow subscribers to process the event (important for
        # cooperative cancellation triggered from subscriber callbacks).
        await asyncio.sleep(0)

    async def _close_subscribers(self) -> None:
        """Send sentinel None to all subscriber queues to signal completion.

        Uses non-blocking put to prevent deadlock when a subscriber
        queue is full and no consumer is draining it.
        """
        async with self._lock:
            for q in self._subscribers:
                # Clear stale events to make room for sentinel
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    logger.warning("Could not send shutdown sentinel — queue still full")

    # --- Agent / sandbox runners ---

    async def _run_architect(
        self, state: AgentState, architect: ArchitectAgent, cost_tracker: CostTracker
    ) -> AgentState:
        logger.info("Running architect agent...")
        input_data: dict[str, Any] = {
            "request": state["request"],
            "project_info": state["project_info"],
            "file_tree": state["file_tree"],
        }
        repo_map = gather_repo_map(
            workspace_root=Path(state["workspace_root"]),
            task={"description": state["request"]},
            file_tree=state["file_tree"],
            languages=(state.get("project_info") or {}).get("languages"),
        )
        if repo_map:
            input_data["repo_map"] = repo_map
        arch_review = state.get("review_report")
        if arch_review:
            architectural_issues = [
                i
                for i in arch_review.get("issues", [])
                if i.get("severity") in ("blocker", "major")
            ]
            if architectural_issues:
                input_data["review_feedback"] = architectural_issues
        try:
            result = await self._invoke(
                architect, input_data, "architect", self.config.architect, cost_tracker, state
            )
            state["plan"] = result
            tasks = result.get("tasks", [])
            if tasks:
                state["current_task"] = tasks[0]
        except Exception as e:
            state["status"] = "error"
            state["error"] = f"Architect failed: {e}"
        return state

    async def _run_implementer(
        self,
        state: AgentState,
        implementer: ImplementerAgent,
        cost_tracker: CostTracker,
    ) -> AgentState:
        logger.info("Running implementer agent (iteration %d)...", state["iteration"])
        review_issues = None
        impl_review = state.get("review_report")
        if impl_review and state["iteration"] > 1:
            review_issues = impl_review.get("issues", [])
        existing_files = gather_context(
            workspace_root=Path(state["workspace_root"]),
            task=state.get("current_task"),
            review_feedback=review_issues,
            file_tree=state.get("file_tree", []),
        )
        project_info = state.get("project_info") or {}
        repo_map = gather_repo_map(
            workspace_root=Path(state["workspace_root"]),
            task=state.get("current_task"),
            review_feedback=review_issues,
            file_tree=state.get("file_tree", []),
            languages=project_info.get("languages"),
        )
        input_data: dict[str, Any] = {
            "task": state.get("current_task"),
            "plan_summary": (state.get("plan") or {}).get("summary", ""),
            "project_info": state["project_info"],
            "existing_files": existing_files if existing_files else None,
            "repo_map": repo_map if repo_map else None,
        }
        if review_issues:
            input_data["review_feedback"] = review_issues
        impl_test_report = state.get("test_report")
        if impl_test_report and not impl_test_report.get("passed"):
            input_data["test_report"] = impl_test_report
        try:
            result = await self._invoke(
                implementer,
                input_data,
                "implementer",
                self.config.implementer,
                cost_tracker,
                state,
            )
            state["current_patch"] = result
            if result.get("status") == "blocked":
                state["status"] = "error"
                state["error"] = "Implementer is blocked: " + "; ".join(
                    result.get("questions", ["Unknown reason"])
                )
        except Exception as e:
            state["status"] = "error"
            state["error"] = f"Implementer failed: {e}"
        return state

    async def _run_sandbox(
        self, state: AgentState, sandbox: BaseSandbox, *, applied_diff: bool = False
    ) -> AgentState:
        """Test the patch.  With *applied_diff* the workspace already holds the diff
        (``_apply_to_workspace``), so it is not applied a second time, and a diff
        that did not apply is not tested at all."""
        logger.info("Running sandbox tests...")
        patch = state.get("current_patch") or {}
        if applied_diff and patch.get("diff_unified"):
            if not state.get("patch_applied"):
                state["test_report"] = {
                    "passed": False,
                    "total": 0,
                    "failures": 0,
                    "stdout": "",
                    "stderr": "The diff did not apply to the workspace; nothing was tested.",
                }
                return state
            patch = {**patch, "diff_unified": ""}
        try:
            result = await sandbox.apply_and_test(patch)
            state["test_report"] = result
        except Exception as e:
            state["test_report"] = {
                "passed": False,
                "total": 0,
                "failures": 0,
                "stderr": str(e),
            }
        return state

    async def _run_reviewer(
        self,
        state: AgentState,
        reviewer: ReviewerAgent,
        cost_tracker: CostTracker,
    ) -> AgentState:
        logger.info("Running reviewer agent...")
        state["previous_review"] = state.get("review_report")
        input_data: dict[str, Any] = {
            "request": state["request"],
            "diff": (state.get("current_patch") or {}).get("diff_unified", ""),
            "test_report": state.get("test_report"),
            "iteration": state["iteration"],
        }
        plan = state.get("plan") or {}
        if "acceptance" in plan:
            input_data["acceptance"] = plan["acceptance"]
        try:
            result = await self._invoke(
                reviewer, input_data, "reviewer", self.config.reviewer, cost_tracker, state
            )
            state["review_report"] = result

            # Never trust reviewer acceptance_met directly — enforce hard gates.
            # Mirror graph.py: actual approval requires patch applied + tests
            # passed + no blocker/major issues.
            reviewer_says_accepted = result.get("acceptance_met", False)
            state["is_approved"] = reviewer_says_accepted and should_accept(
                result,
                state.get("test_report"),
                threshold=self.config.pipeline.emcs_threshold,
                patch_applied=state.get("patch_applied", False),
            )
            if reviewer_says_accepted and not state["is_approved"]:
                logger.info(
                    "Reviewer said accepted, but hard gates failed — overriding to not approved"
                )
        except Exception as e:
            state["status"] = "error"
            state["error"] = f"Reviewer failed: {e}"
        return state

    def _apply_to_workspace(self, state: AgentState, workspace: Workspace) -> None:
        patch = state.get("current_patch") or {}
        diff = patch.get("diff_unified", "")
        if not diff:
            state["patch_applied"] = False
            return
        try:
            applied = workspace.apply_patch(diff)
        except Exception as e:
            logger.warning("Could not apply patch to workspace: %s", e)
            applied = False
        state["patch_applied"] = applied
        if not applied:
            logger.warning("Patch did not apply cleanly to workspace")
            return
        # The patch is on disk whether or not git accepts the commit.
        rationale = " ".join(str(patch.get("rationale") or "code changes").split())[:80]
        try:
            workspace.commit(f"{RUN_COMMIT_PREFIX} {state['iteration']} — {rationale}")
        except Exception as e:
            logger.warning("Workspace commit failed (the patch stays applied): %s", e)

    def _route(self, state: AgentState) -> AgentState:
        from delegatron.orchestrator.router import route_after_review

        # Check for stall — but not when already approved, since
        # approved reviews may share issues and falsely trigger overlap
        current_review = state.get("review_report")
        prev_review = state.get("previous_review")
        is_local_model = is_local_base_url(self.config.implementer.base_url)
        stall_min_iter = 5 if is_local_model else 3
        if (
            not state.get("is_approved")
            and current_review
            and prev_review
            and detect_stall(
                current_review,
                prev_review,
                iteration=state.get("iteration", 0),
                min_iterations=stall_min_iter,
            )
        ):
            state["status"] = "stalled"
            return state

        direction = route_after_review(dict(state))
        if direction == "approved":
            state["status"] = "approved"
        elif direction == "exhausted":
            state["status"] = "exhausted"
        elif direction == "architect":
            # Signal the main loop to re-run the architect before next iteration
            state["_replan"] = True
        elif direction == "implementer":
            pass  # Keep status as "running" — loop continues
        else:
            state["status"] = "error"
            state["error"] = f"Unexpected route direction: {direction}"
            logger.error("Invalid router direction: %s", direction)
        return state
