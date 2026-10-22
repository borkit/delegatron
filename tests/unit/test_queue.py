"""Tests for delegatron.queue — queue-based execution with worker pools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.config import Config
from delegatron.queue import QueueConfig, QueuedRun, RunQueue, RunStatus


def _make_run(
    run_id: str = "test-run-1",
    priority: int = 0,
    request: str = "add login endpoint",
) -> QueuedRun:
    """Helper to create a QueuedRun with sensible defaults."""
    return QueuedRun(
        run_id=run_id,
        request=request,
        config=Config(),
        workspace_root=Path("/tmp/fake-repo"),
        priority=priority,
    )


class TestQueuedRun:
    """Tests for QueuedRun dataclass."""

    def test_default_status_is_queued(self) -> None:
        run = _make_run()
        assert run.status == RunStatus.QUEUED

    def test_priority_ordering(self) -> None:
        high = _make_run(run_id="high", priority=0)
        low = _make_run(run_id="low", priority=10)
        assert high < low

    def test_same_priority_ordered_by_time(self) -> None:
        early = _make_run(run_id="early", priority=0)
        early.queued_at = datetime(2026, 1, 1, tzinfo=UTC)
        late = _make_run(run_id="late", priority=0)
        late.queued_at = datetime(2026, 1, 2, tzinfo=UTC)
        assert early < late


class TestRunQueue:
    """Tests for RunQueue."""

    @pytest.fixture()
    def queue(self) -> RunQueue:
        return RunQueue(QueueConfig(max_workers=2, poll_interval_seconds=0.1))

    @pytest.mark.asyncio()
    async def test_submit_returns_run_id(self, queue: RunQueue) -> None:
        run = _make_run()
        result = await queue.submit(run)
        assert result == "test-run-1"

    @pytest.mark.asyncio()
    async def test_submit_duplicate_raises(self, queue: RunQueue) -> None:
        run1 = _make_run(run_id="dup")
        await queue.submit(run1)
        run2 = _make_run(run_id="dup")
        with pytest.raises(ValueError, match="already exists"):
            await queue.submit(run2)

    @pytest.mark.asyncio()
    async def test_get_status_returns_run(self, queue: RunQueue) -> None:
        run = _make_run()
        await queue.submit(run)
        status = queue.get_status("test-run-1")
        assert status is not None
        assert status.run_id == "test-run-1"
        assert status.status == RunStatus.QUEUED

    def test_get_status_unknown_returns_none(self, queue: RunQueue) -> None:
        assert queue.get_status("nonexistent") is None

    @pytest.mark.asyncio()
    async def test_list_queued(self, queue: RunQueue) -> None:
        await queue.submit(_make_run(run_id="a"))
        await queue.submit(_make_run(run_id="b"))
        queued = queue.list_queued()
        assert len(queued) == 2

    @pytest.mark.asyncio()
    async def test_cancel_queued_run(self, queue: RunQueue) -> None:
        await queue.submit(_make_run(run_id="cancel-me"))
        assert queue.cancel("cancel-me") is True
        run = queue.get_status("cancel-me")
        assert run is not None
        assert run.status == RunStatus.CANCELLED

    @pytest.mark.asyncio()
    async def test_cancel_nonexistent_returns_false(self, queue: RunQueue) -> None:
        assert queue.cancel("nope") is False

    @pytest.mark.asyncio()
    async def test_cancel_running_returns_false(self, queue: RunQueue) -> None:
        run = _make_run(run_id="running-one")
        await queue.submit(run)
        run.status = RunStatus.RUNNING
        assert queue.cancel("running-one") is False

    @pytest.mark.asyncio()
    async def test_worker_processes_run(self, queue: RunQueue) -> None:
        """Workers should pick up queued runs and execute them."""
        mock_state: dict[str, object] = {"status": "approved", "iteration": 1}

        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(return_value=mock_state)

        with patch(
            "delegatron.orchestrator.graph.Pipeline",
            return_value=mock_pipeline,
        ):
            await queue.start_workers()
            run = _make_run(run_id="worker-test")
            await queue.submit(run)

            # Give workers time to process
            for _ in range(50):
                await asyncio.sleep(0.05)
                if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break

            await queue.stop()

        assert run.status == RunStatus.COMPLETED
        assert run.result is not None
        assert run.started_at is not None
        assert run.completed_at is not None

    @pytest.mark.asyncio()
    async def test_worker_uses_graph_pipeline(self, queue: RunQueue) -> None:
        """Queued runs go through graph.Pipeline, not the deprecated AsyncPipeline."""
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(return_value={"status": "approved"})

        with (
            patch(
                "delegatron.orchestrator.graph.Pipeline",
                return_value=mock_pipeline,
            ) as pipeline_cls,
            patch(
                "delegatron.orchestrator.async_runner.AsyncPipeline",
                side_effect=AssertionError("AsyncPipeline must not be used"),
            ) as legacy_cls,
        ):
            await queue.start_workers()
            run = _make_run(run_id="graph-run", request="add logout")
            await queue.submit(run)

            for _ in range(50):
                await asyncio.sleep(0.05)
                if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break

            await queue.stop()

        pipeline_cls.assert_called_once()
        kwargs = pipeline_cls.call_args.kwargs
        assert kwargs["config"] is run.config
        assert kwargs["workspace_root"] == run.workspace_root
        assert kwargs["run_id"] == "graph-run"
        assert kwargs["allow_dirty"] is False
        assert kwargs["base_branch"] == ""
        # an unattended worker never reaches the blocking input() plan prompt
        assert kwargs["approval_callback"]({"tasks": []}) is True
        mock_pipeline.execute.assert_awaited_once_with("add logout")
        legacy_cls.assert_not_called()
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio()
    async def test_worker_handles_failure(self, queue: RunQueue) -> None:
        """Workers should mark failed runs appropriately."""
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with patch(
            "delegatron.orchestrator.graph.Pipeline",
            return_value=mock_pipeline,
        ):
            await queue.start_workers()
            run = _make_run(run_id="fail-test")
            await queue.submit(run)

            for _ in range(50):
                await asyncio.sleep(0.05)
                if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    break

            await queue.stop()

        assert run.status == RunStatus.FAILED
        assert "boom" in run.error

    @pytest.mark.asyncio()
    async def test_priority_ordering_in_workers(self, queue: RunQueue) -> None:
        """Higher priority runs (lower number) should be processed first."""
        execution_order: list[str] = []

        async def mock_execute(request: str) -> dict[str, object]:
            # Record which run_id was executed based on patched pipeline
            execution_order.append(request)
            return {"status": "approved", "iteration": 1}

        # Submit runs before starting workers so ordering is deterministic
        low_priority = _make_run(run_id="low", priority=10, request="low")
        high_priority = _make_run(run_id="high", priority=1, request="high")

        await queue.submit(low_priority)
        await queue.submit(high_priority)

        # Use a single worker to ensure sequential processing
        queue._config.max_workers = 1

        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(side_effect=mock_execute)

        with patch(
            "delegatron.orchestrator.graph.Pipeline",
            return_value=mock_pipeline,
        ):
            await queue.start_workers()

            for _ in range(50):
                await asyncio.sleep(0.05)
                done = all(
                    r.status in (RunStatus.COMPLETED, RunStatus.FAILED)
                    for r in [low_priority, high_priority]
                )
                if done:
                    break

            await queue.stop()

        assert execution_order[0] == "high"
        assert execution_order[1] == "low"

    @pytest.mark.asyncio()
    async def test_stop_is_idempotent(self, queue: RunQueue) -> None:
        await queue.start_workers()
        await queue.stop()
        await queue.stop()  # should not raise

    @pytest.mark.asyncio()
    async def test_list_running(self, queue: RunQueue) -> None:
        run = _make_run(run_id="r1")
        await queue.submit(run)
        run.status = RunStatus.RUNNING
        running = queue.list_running()
        assert len(running) == 1
        assert running[0].run_id == "r1"

    @pytest.mark.asyncio()
    async def test_cancelled_runs_skipped_by_worker(self, queue: RunQueue) -> None:
        """Workers should skip runs that were cancelled while queued."""
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(return_value={"status": "approved", "iteration": 1})

        run = _make_run(run_id="skip-me")
        await queue.submit(run)
        queue.cancel("skip-me")

        with patch(
            "delegatron.orchestrator.graph.Pipeline",
            return_value=mock_pipeline,
        ):
            await queue.start_workers()
            await asyncio.sleep(0.3)
            await queue.stop()

        mock_pipeline.execute.assert_not_called()
        assert run.status == RunStatus.CANCELLED


def _two_task_plan() -> dict[str, object]:
    return {
        "summary": "two tasks",
        "tasks": [
            {
                "task_id": task_id,
                "title": task_id.upper(),
                "description": f"write {task_id}.py",
                "outputs": [f"{task_id}.py"],
                "risk_level": "low",
                "depends_on": depends_on,
            }
            for task_id, depends_on in (("t1", []), ("t2", ["t1"]))
        ],
        "acceptance": {"criteria": ["both modules exist"]},
        "_meta": {"model": "mock-architect"},
    }


@pytest.mark.asyncio()
async def test_queue_runs_multitask_files_patches(tmp_path: Path) -> None:
    """A real graph.Pipeline behind the queue works every task and writes ``files`` patches."""
    from delegatron.config import PipelineConfig, RoleConfig
    from delegatron.detection import ProjectInfo

    implemented: list[str] = []

    async def implement(input_data: dict[str, object]) -> dict[str, object]:
        task_id = str((input_data.get("task") or {}).get("task_id", ""))  # type: ignore[union-attr]
        implemented.append(task_id)
        return {
            "status": "ok",
            "rationale": f"add {task_id}",
            "files": {f"{task_id}.py": f"{task_id.upper()} = 1\n"},
            "diff_unified": "",
            "tests_to_run": [],
            "questions": [],
            "_meta": {"model": "mock-implementer"},
        }

    architect = MagicMock()
    architect.invoke = AsyncMock(side_effect=lambda _data: _two_task_plan())
    implementer = MagicMock()
    implementer.invoke = AsyncMock(side_effect=implement)
    reviewer = MagicMock()
    reviewer.invoke = AsyncMock(
        side_effect=lambda _data: {
            "summary": "ok",
            "acceptance_met": True,
            "issues": [],
            "_meta": {"model": "mock-reviewer"},
        }
    )
    sandbox = MagicMock()
    sandbox.apply_and_test = AsyncMock(
        return_value={"passed": True, "total": 2, "failures": 0, "stdout": "2 passed"}
    )
    config = Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="mock", model="mock-implementer"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(
            max_iterations=3, max_cost_usd=5.0, max_wall_time_minutes=5, sandbox="local"
        ),
    )
    graph_mod = "delegatron.orchestrator.graph"
    queue = RunQueue(QueueConfig(max_workers=1, poll_interval_seconds=0.05))
    run = QueuedRun(run_id="multi", request="two modules", config=config, workspace_root=tmp_path)
    with (
        patch(f"{graph_mod}.create_architect", return_value=architect),
        patch(f"{graph_mod}.create_implementer", return_value=implementer),
        patch(f"{graph_mod}.create_reviewer", return_value=reviewer),
        patch(f"{graph_mod}.create_sandbox", return_value=sandbox),
        patch(f"{graph_mod}.detect_project", return_value=ProjectInfo(languages=["python"])),
        patch(f"{graph_mod}.get_gpu_power_watts", return_value=None),
        patch(f"{graph_mod}.unload_except"),
        patch(f"{graph_mod}.cleanup_after_run"),
    ):
        await queue.start_workers()
        await queue.submit(run)
        for _ in range(200):
            await asyncio.sleep(0.05)
            if run.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break
        await queue.stop()

    assert run.error == ""
    assert implemented == ["t1", "t2"]
    assert (tmp_path / "t1.py").read_text(encoding="utf-8") == "T1 = 1\n"
    assert (tmp_path / "t2.py").read_text(encoding="utf-8") == "T2 = 1\n"
    assert run.result is not None
    assert run.result["patch_applied"] is True
    assert run.result["status"] == "approved"
    assert run.status == RunStatus.COMPLETED
