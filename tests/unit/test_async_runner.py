"""Tests for the async/event-driven pipeline runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.orchestrator.async_runner import (
    AsyncPipeline,
    PipelineEvent,
    PipelineStatus,
)

# AsyncPipeline is deprecated; TestAsyncPipelineDeprecation asserts the warning.
pytestmark = pytest.mark.filterwarnings("ignore:AsyncPipeline is deprecated:DeprecationWarning")

# ---------------------------------------------------------------------------
# PipelineEvent / PipelineStatus unit tests
# ---------------------------------------------------------------------------


class TestPipelineEvent:
    def test_creation(self) -> None:
        ev = PipelineEvent(
            event_type="plan_created",
            iteration=0,
            timestamp=datetime.now(UTC),
            data={"summary": "test"},
        )
        assert ev.event_type == "plan_created"
        assert ev.iteration == 0
        assert isinstance(ev.data, dict)

    def test_event_types(self) -> None:
        for et in [
            "plan_created",
            "iteration_start",
            "patch_generated",
            "tests_complete",
            "review_complete",
            "approved",
            "exhausted",
            "error",
            "cancelled",
        ]:
            ev = PipelineEvent(event_type=et, iteration=1, timestamp=datetime.now(UTC), data={})
            assert ev.event_type == et


class TestPipelineStatus:
    def test_values(self) -> None:
        assert PipelineStatus.IDLE.value == "idle"
        assert PipelineStatus.RUNNING.value == "running"
        assert PipelineStatus.CANCELLED.value == "cancelled"
        assert PipelineStatus.COMPLETED.value == "completed"
        assert PipelineStatus.FAILED.value == "failed"


# ---------------------------------------------------------------------------
# AsyncPipeline tests
# ---------------------------------------------------------------------------


def _make_config() -> Any:
    cfg = MagicMock()
    cfg.pipeline.max_iterations = 1
    cfg.pipeline.max_cost_usd = 5.0
    cfg.pipeline.max_wall_time_minutes = 30
    cfg.pipeline.constraints = None
    cfg.pipeline.interactive = False
    cfg.pipeline.emcs_threshold = 0.8
    cfg.pipeline.enable_arbitration = False
    cfg.architect.prompt_version = None
    cfg.implementer.prompt_version = None
    cfg.reviewer.prompt_version = None
    return cfg


class TestAsyncPipelineDeprecation:
    def test_construction_warns_at_call_site(self, tmp_path: Any) -> None:
        with pytest.warns(DeprecationWarning, match="graph.Pipeline") as record:
            AsyncPipeline(config=_make_config(), workspace_root=tmp_path, run_id="test-dep")
        assert len(record) == 1
        assert record[0].filename == __file__


class TestAsyncPipelineInit:
    def test_initial_status_is_idle(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-init",
        )
        assert pipeline.status() == PipelineStatus.IDLE

    def test_cancel_sets_event(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-cancel",
        )
        assert not pipeline._cancel_event.is_set()
        pipeline.cancel()
        assert pipeline._cancel_event.is_set()


class TestAsyncPipelineEmit:
    @pytest.mark.asyncio
    async def test_emit_sends_to_subscribers(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-emit",
        )

        # Manually add a subscriber queue
        queue: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        pipeline._subscribers.append(queue)

        ev = PipelineEvent(
            event_type="test_event",
            iteration=1,
            timestamp=datetime.now(UTC),
            data={"key": "value"},
        )
        await pipeline._emit(ev)

        result = queue.get_nowait()
        assert result is not None
        assert result.event_type == "test_event"
        assert result.data["key"] == "value"

    @pytest.mark.asyncio
    async def test_emit_fans_out_to_multiple_subscribers(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-fanout",
        )

        q1: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        q2: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        pipeline._subscribers.extend([q1, q2])

        ev = PipelineEvent(event_type="shared", iteration=0, timestamp=datetime.now(UTC), data={})
        await pipeline._emit(ev)

        assert q1.get_nowait().event_type == "shared"
        assert q2.get_nowait().event_type == "shared"


class TestAsyncPipelineSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_receives_events(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-sub",
        )

        collected: list[PipelineEvent] = []

        async def consumer() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)  # Let subscriber start

        # Emit some events
        for i in range(3):
            ev = PipelineEvent(
                event_type=f"event_{i}",
                iteration=i,
                timestamp=datetime.now(UTC),
                data={},
            )
            await pipeline._emit(ev)

        # Signal end
        await pipeline._close_subscribers()
        await asyncio.wait_for(task, timeout=2.0)

        assert len(collected) == 3
        assert [e.event_type for e in collected] == ["event_0", "event_1", "event_2"]

    @pytest.mark.asyncio
    async def test_subscribe_cleanup_on_end(self, tmp_path: Any) -> None:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="test-cleanup",
        )

        async def consumer() -> None:
            async for _ in pipeline.subscribe():
                pass

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)

        assert len(pipeline._subscribers) == 1
        await pipeline._close_subscribers()
        await asyncio.wait_for(task, timeout=2.0)
        assert len(pipeline._subscribers) == 0


# ---------------------------------------------------------------------------
# Integration-style tests: full pipeline execution with all externals mocked
# ---------------------------------------------------------------------------

_MOD = "delegatron.orchestrator.async_runner"


def _fake_architect_result() -> dict[str, Any]:
    return {
        "summary": "Test plan",
        "tasks": [{"task_id": "t1", "title": "Do thing"}],
        "_meta": {"model": "test", "input_tokens": 10, "output_tokens": 20},
    }


def _fake_implementer_result() -> dict[str, Any]:
    return {
        "status": "ok",
        "rationale": "Implemented",
        "diff_unified": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new",
        "_meta": {"model": "test", "input_tokens": 10, "output_tokens": 20},
    }


def _fake_reviewer_result(*, accepted: bool = True) -> dict[str, Any]:
    return {
        "acceptance_met": accepted,
        "issues": [],
        "_meta": {"model": "test", "input_tokens": 10, "output_tokens": 20},
    }


def _setup_mocks(
    *, architect_error: bool = False, reviewer_accepted: bool = True
) -> dict[str, Any]:
    """Create mock objects for all external dependencies."""
    proj = MagicMock()
    proj.languages = ["python"]
    proj.frameworks = []
    proj.test_commands = ["pytest"]
    proj.lint_commands = ["ruff check ."]
    proj.package_manager = "pip"

    ws = MagicMock()
    ws.get_file_tree.return_value = ["src/main.py"]
    ws.initialize.return_value = MagicMock(branch="run-abc", commit_sha="deadbeef1234")
    ws.apply_patch.return_value = True
    ws.commit.return_value = "abc12345"

    sandbox_inst = MagicMock()  # BaseSandbox.cleanup() is synchronous
    sandbox_inst.apply_and_test = AsyncMock(
        return_value={"passed": True, "total": 3, "failures": 0}
    )

    arch_agent = MagicMock()
    if architect_error:
        arch_agent.invoke = AsyncMock(side_effect=RuntimeError("LLM down"))
    else:
        arch_agent.invoke = AsyncMock(return_value=_fake_architect_result())

    impl_agent = MagicMock()
    impl_agent.invoke = AsyncMock(return_value=_fake_implementer_result())

    rev_agent = MagicMock()
    rev_agent.invoke = AsyncMock(return_value=_fake_reviewer_result(accepted=reviewer_accepted))

    return {
        "project_info": proj,
        "workspace": ws,
        "sandbox": sandbox_inst,
        "architect_agent": arch_agent,
        "implementer_agent": impl_agent,
        "reviewer_agent": rev_agent,
    }


def _mock_security_result() -> MagicMock:
    """Create a mock SecurityResult with no findings."""
    result = MagicMock()
    result.has_blockers = False
    result.summary_events = []
    result.store_events = []
    return result


def _enter_patches(mocks: dict[str, Any], route_return: str = "approved") -> list[Any]:
    """Start all context-manager patches; return list of active patches."""
    specs = [
        patch(f"{_MOD}.detect_project", return_value=mocks["project_info"]),
        patch(f"{_MOD}.Workspace", return_value=mocks["workspace"]),
        patch(f"{_MOD}.create_sandbox", return_value=mocks["sandbox"]),
        patch(f"{_MOD}.create_architect", return_value=mocks["architect_agent"]),
        patch(f"{_MOD}.create_implementer", return_value=mocks["implementer_agent"]),
        patch(f"{_MOD}.create_reviewer", return_value=mocks["reviewer_agent"]),
        patch(f"{_MOD}.ArtifactStore"),
        patch(f"{_MOD}.CostTracker"),
        patch(f"{_MOD}.gather_context", return_value={}),
        patch(f"{_MOD}.should_terminate", return_value=False),
        patch(f"{_MOD}.detect_stall", return_value=False),
        patch(f"{_MOD}.get_score_summary", return_value={"score": 0.9}),
        patch(f"{_MOD}.should_accept", return_value=False),
        patch(
            "delegatron.orchestrator.router.route_after_review",
            return_value=route_return,
        ),
        patch(f"{_MOD}.run_security_checks", return_value=_mock_security_result()),
    ]
    for p in specs:
        p.__enter__()
    return specs


def _exit_patches(specs: list[Any]) -> None:
    for p in specs:
        p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_execute_emits_expected_events(tmp_path: Path) -> None:
    """A single-iteration approved run emits the core lifecycle events."""
    mocks = _setup_mocks()
    specs = _enter_patches(mocks)
    try:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-001",
            artifacts_dir=tmp_path / "runs",
        )
        collected: list[PipelineEvent] = []

        async def collect() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        state = await pipeline.execute("add login")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    assert state["status"] == "approved"
    types = [e.event_type for e in collected]
    for expected in (
        "plan_created",
        "iteration_start",
        "patch_generated",
        "tests_complete",
        "review_complete",
        "approved",
    ):
        assert expected in types, f"Missing event: {expected}"


@pytest.mark.asyncio
async def test_events_arrive_in_pipeline_order(tmp_path: Path) -> None:
    """Events appear in the correct chronological pipeline order."""
    mocks = _setup_mocks()
    specs = _enter_patches(mocks)
    try:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-002",
            artifacts_dir=tmp_path / "runs",
        )
        collected: list[PipelineEvent] = []

        async def collect() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await pipeline.execute("fix bug")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    expected_order = [
        "plan_created",
        "iteration_start",
        "patch_generated",
        "tests_complete",
        "review_complete",
        "approved",
    ]
    types = [e.event_type for e in collected]
    indices = [types.index(et) for et in expected_order]
    assert indices == sorted(indices), f"Out of order: {types}"


@pytest.mark.asyncio
async def test_cancel_stops_execution(tmp_path: Path) -> None:
    """Cancelling after plan_created stops the pipeline."""
    config = _make_config()
    config.pipeline.max_iterations = 10

    mocks = _setup_mocks()
    # Route to implementer so the loop would keep going
    specs = _enter_patches(mocks, route_return="implementer")
    try:
        pipeline = AsyncPipeline(
            config=config,
            workspace_root=tmp_path,
            run_id="int-003",
            artifacts_dir=tmp_path / "runs",
        )
        collected: list[PipelineEvent] = []

        async def collect() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)
                if ev.event_type == "plan_created":
                    pipeline.cancel()

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        state = await pipeline.execute("refactor auth")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    assert state["status"] == "cancelled"
    assert pipeline.status() == PipelineStatus.CANCELLED
    types = [e.event_type for e in collected]
    assert "cancelled" in types


@pytest.mark.asyncio
async def test_multiple_subscribers_receive_same_events(
    tmp_path: Path,
) -> None:
    """Two subscribers each get all events (fan-out)."""
    mocks = _setup_mocks()
    specs = _enter_patches(mocks)
    try:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-004",
            artifacts_dir=tmp_path / "runs",
        )
        collected_a: list[PipelineEvent] = []
        collected_b: list[PipelineEvent] = []

        async def into(target: list[PipelineEvent]) -> None:
            async for ev in pipeline.subscribe():
                target.append(ev)

        ta = asyncio.create_task(into(collected_a))
        tb = asyncio.create_task(into(collected_b))
        await asyncio.sleep(0)
        await pipeline.execute("add feature")
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=5.0)
    finally:
        _exit_patches(specs)

    types_a = [e.event_type for e in collected_a]
    types_b = [e.event_type for e in collected_b]
    assert types_a == types_b
    assert len(collected_a) > 0


@pytest.mark.asyncio
async def test_error_emits_error_event(tmp_path: Path) -> None:
    """Architect failure -> error event emitted, status FAILED."""
    mocks = _setup_mocks(architect_error=True)
    specs = _enter_patches(mocks)
    try:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-005",
            artifacts_dir=tmp_path / "runs",
        )
        collected: list[PipelineEvent] = []

        async def collect() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        state = await pipeline.execute("broken")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    assert state["status"] == "error"
    assert pipeline.status() == PipelineStatus.FAILED
    error_events = [e for e in collected if e.event_type == "error"]
    assert len(error_events) >= 1


@pytest.mark.asyncio
async def test_completed_status_after_successful_run(
    tmp_path: Path,
) -> None:
    """Pipeline status is COMPLETED after a successful execution."""
    mocks = _setup_mocks()
    specs = _enter_patches(mocks)
    try:
        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-006",
            artifacts_dir=tmp_path / "runs",
        )

        async def drain() -> None:
            async for _ in pipeline.subscribe():
                pass

        task = asyncio.create_task(drain())
        await asyncio.sleep(0)
        await pipeline.execute("test")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    assert pipeline.status() == PipelineStatus.COMPLETED


@pytest.mark.asyncio
async def test_bootstrap_failure_sets_error_state(tmp_path: Path) -> None:
    """Agent creation failure -> error status, is_approved=False, FAILED."""
    mocks = _setup_mocks()
    # Override create_architect to raise
    specs = _enter_patches(mocks)
    try:
        # Replace the create_architect patch with one that raises
        # Find and update the create_architect spec
        for i, p in enumerate(specs):
            if hasattr(p, "attribute") and p.attribute == "create_architect":
                p.__exit__(None, None, None)
                specs[i] = patch(
                    f"{_MOD}.create_architect",
                    side_effect=RuntimeError("Provider bootstrap failed"),
                )
                specs[i].__enter__()
                break
        else:
            # Patch wasn't found by attribute, exit and re-enter with error
            _exit_patches(specs)
            specs = _enter_patches(mocks)
            # Override create_architect
            for _i, p in enumerate(specs):
                p.__exit__(None, None, None)
            specs = [
                patch(f"{_MOD}.detect_project", return_value=mocks["project_info"]),
                patch(f"{_MOD}.Workspace", return_value=mocks["workspace"]),
                patch(f"{_MOD}.create_sandbox", return_value=mocks["sandbox"]),
                patch(
                    f"{_MOD}.create_architect",
                    side_effect=RuntimeError("Provider bootstrap failed"),
                ),
                patch(f"{_MOD}.create_implementer", return_value=mocks["implementer_agent"]),
                patch(f"{_MOD}.create_reviewer", return_value=mocks["reviewer_agent"]),
                patch(f"{_MOD}.ArtifactStore"),
                patch(f"{_MOD}.CostTracker"),
                patch(f"{_MOD}.gather_context", return_value={}),
                patch(f"{_MOD}.should_terminate", return_value=False),
                patch(f"{_MOD}.detect_stall", return_value=False),
                patch(f"{_MOD}.get_score_summary", return_value={"score": 0.9}),
                patch(f"{_MOD}.should_accept", return_value=False),
                patch("delegatron.orchestrator.router.route_after_review", return_value="approved"),
                patch(f"{_MOD}.run_security_checks", return_value=_mock_security_result()),
            ]
            for p in specs:
                p.__enter__()

        pipeline = AsyncPipeline(
            config=_make_config(),
            workspace_root=tmp_path,
            run_id="int-boot",
            artifacts_dir=tmp_path / "runs",
        )
        collected: list[PipelineEvent] = []

        async def collect() -> None:
            async for ev in pipeline.subscribe():
                collected.append(ev)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        state = await pipeline.execute("bootstrap fail")
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        _exit_patches(specs)

    assert state["status"] == "error"
    assert state["is_approved"] is False
    assert pipeline.status() == PipelineStatus.FAILED
    error_events = [e for e in collected if e.event_type == "error"]
    assert len(error_events) >= 1
    assert "bootstrap" in error_events[0].data.get("error", "").lower()


# ---------------------------------------------------------------------------
# Workspace, apply/commit, sandbox, EMCS and cost wiring (#87, #88, #94, #95,
# #103, #112, #115, #124, #127)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _dirty_repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_bytes(b"x = 1\n")
    _git(root, "add", "app.py")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    (root / "notes.txt").write_bytes(b"mine\n")
    return root


class TestWorkspaceFailsClosed:
    @pytest.mark.asyncio
    async def test_dirty_repo_ends_the_run_before_any_agent_call(self, tmp_path: Path) -> None:
        repo = _dirty_repo(tmp_path / "repo")
        sub = repo / "pkg"  # a subdirectory of a repository is a git workspace too
        sub.mkdir()
        (sub / "scratch.py").write_bytes(b"wip = 1\n")
        mocks = _setup_mocks()
        with (
            patch(f"{_MOD}.detect_project", return_value=mocks["project_info"]),
            patch(f"{_MOD}.create_sandbox", return_value=mocks["sandbox"]),
            patch(f"{_MOD}.create_architect", return_value=mocks["architect_agent"]),
            patch(f"{_MOD}.create_implementer", return_value=mocks["implementer_agent"]),
            patch(f"{_MOD}.create_reviewer", return_value=mocks["reviewer_agent"]),
        ):
            pipeline = AsyncPipeline(
                config=_make_config(),
                workspace_root=sub,
                run_id="dirty",
                artifacts_dir=tmp_path / "runs",
            )
            collected: list[PipelineEvent] = []

            async def collect() -> None:
                async for ev in pipeline.subscribe():
                    collected.append(ev)

            task = asyncio.create_task(collect())
            await asyncio.sleep(0)
            state = await pipeline.execute("add login")
            await asyncio.wait_for(task, timeout=5.0)

        assert state["status"] == "error"
        assert "scratch.py" in state["error"]
        assert pipeline.status() == PipelineStatus.FAILED
        mocks["architect_agent"].invoke.assert_not_awaited()
        mocks["implementer_agent"].invoke.assert_not_awaited()
        assert [e.event_type for e in collected] == ["error"]
        assert _git(repo, "branch", "--show-current") == "main"

    @pytest.mark.asyncio
    async def test_allow_dirty_and_a_fresh_branch_are_requested(self, tmp_path: Path) -> None:
        mocks = _setup_mocks()
        # The patched Workspace finds a repository, so the run branch is set up.
        specs = _enter_patches(mocks)
        try:
            pipeline = AsyncPipeline(
                config=_make_config(),
                workspace_root=tmp_path,
                run_id="dirty-ok",
                artifacts_dir=tmp_path / "runs",
                allow_dirty=True,
            )
            await pipeline.execute("add login")
        finally:
            _exit_patches(specs)

        mocks["workspace"].initialize.assert_called_once_with(allow_dirty=True, fresh=True)
        mocks["sandbox"].cleanup.assert_called_once_with()


class TestApplyAndSandbox:
    def test_refused_commit_keeps_the_written_patch_applied(self, tmp_path: Path) -> None:
        pipeline = AsyncPipeline(config=_make_config(), workspace_root=tmp_path, run_id="c")
        workspace = MagicMock()
        workspace.apply_patch.return_value = True
        workspace.commit.side_effect = RuntimeError("git commit failed (rc=1): hook said no")
        state: dict[str, Any] = {
            "iteration": 2,
            "current_patch": {"diff_unified": "--- a/x\n+++ b/x\n", "rationale": "fix  it"},
        }

        pipeline._apply_to_workspace(state, workspace)  # type: ignore[arg-type]

        assert state["patch_applied"] is True
        message = workspace.commit.call_args.args[0]
        assert message == "chore(delegatron): iteration 2 — fix it"

    @pytest.mark.asyncio
    async def test_sandbox_does_not_apply_an_applied_diff_twice(self, tmp_path: Path) -> None:
        pipeline = AsyncPipeline(config=_make_config(), workspace_root=tmp_path, run_id="s")
        sandbox = MagicMock()
        sandbox.apply_and_test = AsyncMock(return_value={"passed": True, "total": 1})
        patch_set = {"diff_unified": "--- a/x\n+++ b/x\n", "files": {}}
        state: dict[str, Any] = {"current_patch": patch_set, "patch_applied": True}

        await pipeline._run_sandbox(state, sandbox, applied_diff=True)  # type: ignore[arg-type]

        assert sandbox.apply_and_test.await_args.args[0]["diff_unified"] == ""
        assert state["current_patch"]["diff_unified"]  # the stored patch keeps its diff

        sandbox.apply_and_test.reset_mock()
        failed: dict[str, Any] = {"current_patch": patch_set, "patch_applied": False}
        await pipeline._run_sandbox(failed, sandbox, applied_diff=True)  # type: ignore[arg-type]

        sandbox.apply_and_test.assert_not_awaited()
        assert failed["test_report"]["passed"] is False
        assert "did not apply" in failed["test_report"]["stderr"]


def _real_config(**pipeline: Any) -> Any:
    from delegatron.config import Config, PipelineConfig, RoleConfig, SecurityConfig

    settings: dict[str, Any] = {"max_iterations": 1, "max_cost_usd": 5.0, "sandbox": "local"}
    settings.update(pipeline)
    return Config(
        architect=RoleConfig(provider="mock", model="mock-architect"),
        implementer=RoleConfig(provider="openai", model="gpt-4.1"),
        reviewer=RoleConfig(provider="mock", model="mock-reviewer"),
        pipeline=PipelineConfig(**settings),
        security=SecurityConfig(secrets_allowlist=["sk-test-allowed"]),
    )


class TestEmcsAndCalls:
    @pytest.mark.asyncio
    async def test_minor_only_rejection_is_overridden_without_rewriting_the_review(
        self, tmp_path: Path
    ) -> None:
        mocks = _setup_mocks()
        minor = {"severity": "minor", "title": "naming", "details": "", "recommended_fix": ""}
        mocks["reviewer_agent"].invoke = AsyncMock(
            return_value={
                "acceptance_met": False,
                "issues": [minor],
                "_meta": {"model": "mock-reviewer"},
            }
        )
        mocks["sandbox"].apply_and_test = AsyncMock(
            return_value={"passed": True, "total": 3, "failures": 0, "stdout": "3 passed"}
        )
        security = _mock_security_result()
        with (
            patch(f"{_MOD}.detect_project", return_value=mocks["project_info"]),
            patch(f"{_MOD}.Workspace", return_value=mocks["workspace"]),
            patch(f"{_MOD}.create_sandbox", return_value=mocks["sandbox"]),
            patch(f"{_MOD}.create_architect", return_value=mocks["architect_agent"]),
            patch(f"{_MOD}.create_implementer", return_value=mocks["implementer_agent"]),
            patch(f"{_MOD}.create_reviewer", return_value=mocks["reviewer_agent"]),
            patch(f"{_MOD}.gather_context", return_value={}),
            patch(f"{_MOD}.gather_repo_map", return_value="app.py: def main()"),
            patch(f"{_MOD}.get_gpu_power_watts", return_value=None),
            patch(f"{_MOD}.run_security_checks", return_value=security) as security_checks,
        ):
            config = _real_config()
            pipeline = AsyncPipeline(
                config=config,
                workspace_root=tmp_path,
                run_id="emcs",
                artifacts_dir=tmp_path / "runs",
            )
            state = await pipeline.execute("add login")

        assert state["status"] == "approved"
        assert state["review_report"]["acceptance_met"] is False  # never rewritten
        overrides = [m for m in state["messages"] if m.get("event") == "emcs_override"]
        assert len(overrides) == 1
        events = (tmp_path / "runs" / "emcs" / "events.jsonl").read_text(encoding="utf-8")
        assert '"emcs_override"' in events
        score = next(m for m in state["messages"] if m.get("event") == "emcs_score")
        assert score["threshold"] == config.pipeline.emcs_threshold
        kwargs = security_checks.call_args.kwargs
        assert kwargs["secrets_allowlist"] == ["sk-test-allowed"]
        architect_input = mocks["architect_agent"].invoke.await_args.args[0]
        assert architect_input["repo_map"] == "app.py: def main()"

    @pytest.mark.asyncio
    async def test_empty_rejection_with_passing_tests_is_not_approved(self, tmp_path: Path) -> None:
        mocks = _setup_mocks(reviewer_accepted=False)
        mocks["sandbox"].apply_and_test = AsyncMock(
            return_value={"passed": True, "total": 3, "failures": 0, "stdout": "3 passed"}
        )
        with (
            patch(f"{_MOD}.detect_project", return_value=mocks["project_info"]),
            patch(f"{_MOD}.Workspace", return_value=mocks["workspace"]),
            patch(f"{_MOD}.create_sandbox", return_value=mocks["sandbox"]),
            patch(f"{_MOD}.create_architect", return_value=mocks["architect_agent"]),
            patch(f"{_MOD}.create_implementer", return_value=mocks["implementer_agent"]),
            patch(f"{_MOD}.create_reviewer", return_value=mocks["reviewer_agent"]),
            patch(f"{_MOD}.gather_context", return_value={}),
            patch(f"{_MOD}.get_gpu_power_watts", return_value=None),
            patch(f"{_MOD}.run_security_checks", return_value=_mock_security_result()),
        ):
            pipeline = AsyncPipeline(
                config=_real_config(),
                workspace_root=tmp_path,
                run_id="empty",
                artifacts_dir=tmp_path / "runs",
            )
            state = await pipeline.execute("add login")

        assert state["is_approved"] is False
        assert state["status"] != "approved"

    @pytest.mark.asyncio
    async def test_calls_are_costed_by_endpoint_with_watts_sampled_after_generation(
        self, tmp_path: Path
    ) -> None:
        order: list[str] = []
        mocks = _setup_mocks()

        async def invoke(_data: dict[str, Any]) -> dict[str, Any]:
            order.append("generate")
            return {
                **_fake_architect_result(),
                "_meta": {
                    "model": "mock-architect",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_tokens": 40,
                    "temperature": 0.1,
                },
            }

        mocks["architect_agent"].invoke = AsyncMock(side_effect=invoke)
        tracker = MagicMock()
        tracker.total_cost_usd = 0.0

        def watts() -> float:
            order.append("watts")
            return 123.0

        pipeline = AsyncPipeline(config=_real_config(), workspace_root=tmp_path, run_id="w")
        state: dict[str, Any] = {
            "request": "r",
            "project_info": {"languages": []},
            "file_tree": [],
            "workspace_root": str(tmp_path),
            "iteration": 0,
            "review_report": None,
        }
        with (
            patch(f"{_MOD}.get_gpu_power_watts", side_effect=watts),
            patch(f"{_MOD}.gather_repo_map", return_value=""),
        ):
            await pipeline._run_architect(state, mocks["architect_agent"], tracker)  # type: ignore[arg-type]

        assert order == ["generate", "watts"]
        kwargs = tracker.record.call_args.kwargs
        assert kwargs["is_local"] is True
        assert kwargs["gpu_watts"] == 123.0
        assert kwargs["generation_seconds"] >= 0
        assert kwargs["cache_read_tokens"] == 40
        assert kwargs["temperature"] == 0.1
