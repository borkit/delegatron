"""Queue-based execution with worker pools for Delegatron.

Enables submitting multiple pipeline runs that execute concurrently
via an asyncio worker pool, with priority ordering and status tracking.
Each run executes through :class:`delegatron.orchestrator.graph.Pipeline`,
the same orchestrator the CLI and API server use.

Usage::

    queue_config = QueueConfig(max_workers=3)
    run_queue = RunQueue(queue_config)
    await run_queue.start_workers()

    run = QueuedRun(
        run_id="abc123",
        request="add login endpoint",
        config=config,
        workspace_root=Path("/repo"),
    )
    await run_queue.submit(run)

    # Later...
    status = run_queue.get_status("abc123")
    await run_queue.stop()
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from delegatron.config import Config

logger = logging.getLogger(__name__)


class RunStatus(Enum):
    """Status of a queued run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueueConfig:
    """Configuration for the run queue.

    Attributes:
        max_workers: Maximum number of concurrent pipeline executions.
        poll_interval_seconds: How often idle workers check the queue.
        max_queue_size: Maximum number of runs that can be queued.
    """

    max_workers: int = 3
    poll_interval_seconds: float = 1.0
    max_queue_size: int = 100


@dataclass
class QueuedRun:
    """A pipeline run waiting in or being processed by the queue.

    Attributes:
        run_id: Unique identifier for this run.
        request: The natural-language task description.
        config: Delegatron configuration to use.
        workspace_root: Path to the workspace/repo for this run.
        priority: Priority level (lower number = higher priority).
        queued_at: When the run was submitted.
        started_at: When execution began (None if still queued).
        completed_at: When execution finished (None if not done).
        status: Current status of the run.
        result: Pipeline result data when completed.
        error: Error message if the run failed.
        allow_dirty: Start even when the git workspace has uncommitted changes.
        base_branch: Branch the run branch forks from ("" = the checked-out branch).
    """

    run_id: str
    request: str
    config: Config
    workspace_root: Path
    priority: int = 0
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: RunStatus = RunStatus.QUEUED
    result: dict[str, Any] | None = None
    error: str = ""
    allow_dirty: bool = False
    base_branch: str = ""

    def __lt__(self, other: QueuedRun) -> bool:
        """Compare by priority for PriorityQueue ordering."""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.queued_at < other.queued_at


def _auto_approve_plan(_plan: dict[str, Any]) -> bool:
    """Plan approval for unattended runs: always approve."""
    return True


class RunQueue:
    """Manages a pool of workers that process pipeline runs from a priority queue.

    Runs are ordered by priority (lower number = higher priority), with
    ties broken by submission time (earlier first).
    """

    def __init__(self, config: QueueConfig) -> None:
        self._config = config
        self._queue: asyncio.PriorityQueue[QueuedRun] = asyncio.PriorityQueue(
            maxsize=config.max_queue_size,
        )
        self._runs: dict[str, QueuedRun] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def submit(self, run: QueuedRun) -> str:
        """Add a run to the queue.

        Args:
            run: The QueuedRun to enqueue.

        Returns:
            The run_id of the submitted run.

        Raises:
            asyncio.QueueFull: If the queue is at max capacity.
            ValueError: If a run with the same run_id already exists.
        """
        async with self._lock:
            if run.run_id in self._runs:
                msg = f"Run with id '{run.run_id}' already exists"
                raise ValueError(msg)
            run.status = RunStatus.QUEUED
            run.queued_at = datetime.now(UTC)
            try:
                self._queue.put_nowait(run)
            except asyncio.QueueFull:
                raise  # Don't register the run if queue is full
            self._runs[run.run_id] = run
        logger.info("Run %s queued (priority=%d)", run.run_id, run.priority)
        return run.run_id

    async def start_workers(self) -> None:
        """Spawn worker tasks that pull from the queue and execute runs."""
        self._shutdown_event.clear()
        for i in range(self._config.max_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info("Started %d queue workers", self._config.max_workers)

    async def stop(self) -> None:
        """Gracefully shut down all workers.

        Workers finish their current run before stopping. Queued runs
        remain in the queue with their current status.
        """
        self._shutdown_event.set()
        # Give workers time to finish current work
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._workers, return_exceptions=True),
                timeout=30.0,
            )
        except TimeoutError:
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("All queue workers stopped")

    def get_status(self, run_id: str) -> QueuedRun | None:
        """Get the current state of a run by its ID.

        Returns None if the run_id is not found.
        """
        return self._runs.get(run_id)

    def list_queued(self) -> list[QueuedRun]:
        """Return all runs currently waiting in the queue."""
        return [r for r in self._runs.values() if r.status == RunStatus.QUEUED]

    def list_running(self) -> list[QueuedRun]:
        """Return all runs currently being executed."""
        return [r for r in self._runs.values() if r.status == RunStatus.RUNNING]

    def cancel(self, run_id: str) -> bool:
        """Cancel a run that is still waiting in the queue.

        Only runs with status QUEUED can be cancelled.  A run that is already
        executing cannot be cancelled through the queue: the queue keeps no
        handle to its pipeline or worker task.

        Returns True if the run was cancelled, False otherwise.
        """
        run = self._runs.get(run_id)
        if run is None:
            return False
        if run.status != RunStatus.QUEUED:
            return False
        run.status = RunStatus.CANCELLED
        run.completed_at = datetime.now(UTC)
        logger.info("Run %s cancelled", run_id)
        return True

    async def _worker(self, worker_id: int) -> None:
        """Worker loop: pull runs from the queue and execute them."""
        logger.debug("Worker %d started", worker_id)
        while not self._shutdown_event.is_set():
            try:
                run = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=self._config.poll_interval_seconds,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Skip cancelled runs
            if run.status == RunStatus.CANCELLED:
                self._queue.task_done()
                continue

            run.status = RunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            logger.info("Worker %d executing run %s", worker_id, run.run_id)

            try:
                from delegatron.orchestrator.graph import Pipeline

                pipeline = Pipeline(
                    config=run.config,
                    workspace_root=run.workspace_root,
                    run_id=run.run_id,
                    base_branch=run.base_branch,
                    # An unattended worker never blocks the event loop on input().
                    approval_callback=_auto_approve_plan,
                    allow_dirty=run.allow_dirty,
                )
                state = await pipeline.execute(run.request)
                run.result = copy.deepcopy(dict(state))
                run.completed_at = datetime.now(UTC)

                # Map pipeline terminal state to queue status —
                # don't blindly mark everything as COMPLETED.
                pipeline_status = state.get("status", "")
                if pipeline_status in ("error",):
                    run.status = RunStatus.FAILED
                    run.error = state.get("error", "Pipeline ended with error status")
                    logger.warning("Run %s pipeline error: %s", run.run_id, run.error)
                elif pipeline_status in ("cancelled",):
                    run.status = RunStatus.CANCELLED
                    logger.info("Run %s cancelled", run.run_id)
                else:
                    # approved, exhausted, stalled — pipeline finished normally
                    run.status = RunStatus.COMPLETED
                    logger.info("Run %s completed (status=%s)", run.run_id, pipeline_status)
            except Exception as exc:
                run.status = RunStatus.FAILED
                run.error = str(exc)
                run.completed_at = datetime.now(UTC)
                logger.exception("Run %s failed: %s", run.run_id, exc)
            finally:
                self._queue.task_done()
