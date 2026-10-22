"""Tests for project mode — multi-milestone orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.project import (
    DEFAULT_MAX_MILESTONES,
    Milestone,
    MilestoneResult,
    ProjectPlan,
    ProjectReport,
    ProjectRunner,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(architect_model: str = "claude-sonnet-4-20250514") -> Config:
    return Config(
        architect=RoleConfig(provider="mock", model=architect_model),
        implementer=RoleConfig(provider="mock", model="mock-impl"),
        reviewer=RoleConfig(provider="mock", model="mock-rev"),
        pipeline=PipelineConfig(),
    )


def _make_milestone(
    id: str = "m1",
    title: str = "Setup",
    description: str = "Initialize project",
    order: int = 1,
) -> Milestone:
    return Milestone(
        id=id,
        title=title,
        description=description,
        acceptance_criteria=["tests pass"],
        depends_on=[],
        order=order,
    )


def _make_result(
    milestone_id: str = "m1",
    title: str = "Setup",
    status: str = "approved",
    cost: float = 0.5,
) -> MilestoneResult:
    return MilestoneResult(
        milestone_id=milestone_id,
        title=title,
        status=status,
        iterations=2,
        cost_usd=cost,
        elapsed_seconds=12.345,
        emcs_score=0.87654,
        files_created=["app.py"],
    )


# ===================================================================
# 1. Milestone dataclass
# ===================================================================


class TestMilestone:
    def test_construction_defaults(self):
        m = Milestone(id="m1", title="Init", description="Create project skeleton")
        assert m.id == "m1"
        assert m.acceptance_criteria == []
        assert m.depends_on == []
        assert m.order == 0

    def test_to_dict(self):
        m = _make_milestone()
        d = m.to_dict()
        assert d["id"] == "m1"
        assert d["title"] == "Setup"
        assert d["acceptance_criteria"] == ["tests pass"]
        assert d["order"] == 1

    def test_from_dict_round_trip(self):
        original = _make_milestone()
        d = original.to_dict()
        restored = Milestone.from_dict(d)
        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.acceptance_criteria == original.acceptance_criteria
        assert restored.depends_on == original.depends_on
        assert restored.order == original.order

    def test_from_dict_missing_fields(self):
        m = Milestone.from_dict({"title": "Only title"})
        assert m.id == ""
        assert m.title == "Only title"
        assert m.description == ""
        assert m.acceptance_criteria == []
        assert m.depends_on == []
        assert m.order == 0

    def test_from_dict_empty(self):
        m = Milestone.from_dict({})
        assert m.id == ""
        assert m.title == ""


# ===================================================================
# 2. MilestoneResult dataclass
# ===================================================================


class TestMilestoneResult:
    def test_construction(self):
        r = _make_result()
        assert r.milestone_id == "m1"
        assert r.status == "approved"
        assert r.error == ""

    def test_to_dict_rounding(self):
        r = MilestoneResult(
            milestone_id="m1",
            title="Setup",
            status="approved",
            iterations=3,
            cost_usd=1.123456789,
            elapsed_seconds=42.678912,
            emcs_score=0.876549,
        )
        d = r.to_dict()
        assert d["cost_usd"] == round(1.123456789, 6)
        assert d["elapsed_seconds"] == round(42.678912, 1)
        assert d["emcs_score"] == round(0.876549, 3)

    def test_to_dict_includes_error(self):
        r = MilestoneResult(
            milestone_id="m1",
            title="Bad",
            status="error",
            iterations=0,
            cost_usd=0.0,
            elapsed_seconds=0.1,
            emcs_score=0.0,
            error="Something broke",
        )
        assert r.to_dict()["error"] == "Something broke"

    def test_to_dict_files(self):
        r = _make_result()
        assert r.to_dict()["files_created"] == ["app.py"]


# ===================================================================
# 3. ProjectPlan dataclass
# ===================================================================


class TestProjectPlan:
    def test_construction(self):
        plan = ProjectPlan(summary="Build an app")
        assert plan.summary == "Build an app"
        assert plan.milestones == []
        assert plan.total_estimated_iterations == 0

    def test_to_dict_with_milestones(self):
        m1 = _make_milestone(id="m1", title="Auth", order=1)
        m2 = _make_milestone(id="m2", title="DB", order=2)
        plan = ProjectPlan(
            summary="Todo app",
            milestones=[m1, m2],
            total_estimated_iterations=10,
        )
        d = plan.to_dict()
        assert d["summary"] == "Todo app"
        assert len(d["milestones"]) == 2
        assert d["milestones"][0]["id"] == "m1"
        assert d["milestones"][1]["id"] == "m2"
        assert d["total_estimated_iterations"] == 10

    def test_to_dict_empty(self):
        plan = ProjectPlan(summary="Empty")
        d = plan.to_dict()
        assert d["milestones"] == []


# ===================================================================
# 4. ProjectReport dataclass
# ===================================================================


class TestProjectReport:
    def test_milestones_completed_only_approved(self):
        report = ProjectReport(
            project_id="p1",
            request="Build app",
            preset="default",
            results=[
                _make_result(milestone_id="m1", status="approved"),
                _make_result(milestone_id="m2", status="exhausted"),
                _make_result(milestone_id="m3", status="approved"),
            ],
        )
        assert report.milestones_completed == 2

    def test_milestones_total_from_plan(self):
        plan = ProjectPlan(
            summary="X",
            milestones=[_make_milestone(id=f"m{i}") for i in range(5)],
        )
        report = ProjectReport(
            project_id="p1",
            request="X",
            preset="default",
            plan=plan,
        )
        assert report.milestones_total == 5

    def test_milestones_total_no_plan(self):
        report = ProjectReport(project_id="p1", request="X", preset="default")
        assert report.milestones_total == 0

    def test_success_rate(self):
        report = ProjectReport(
            project_id="p1",
            request="X",
            preset="default",
            results=[
                _make_result(status="approved"),
                _make_result(status="approved"),
                _make_result(status="exhausted"),
                _make_result(status="error"),
            ],
        )
        assert report.success_rate == pytest.approx(0.5)

    def test_success_rate_no_results(self):
        report = ProjectReport(project_id="p1", request="X", preset="default")
        assert report.success_rate == 0.0

    def test_success_rate_all_approved(self):
        report = ProjectReport(
            project_id="p1",
            request="X",
            preset="default",
            results=[_make_result(status="approved") for _ in range(3)],
        )
        assert report.success_rate == pytest.approx(1.0)

    def test_to_dict_full(self):
        plan = ProjectPlan(
            summary="Full project",
            milestones=[_make_milestone()],
        )
        report = ProjectReport(
            project_id="p1",
            request="Build it",
            preset="default",
            plan=plan,
            results=[_make_result()],
            status="completed",
            total_cost_usd=1.23456789,
            total_time_seconds=99.999,
            started_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:40",
        )
        d = report.to_dict()
        assert d["project_id"] == "p1"
        assert d["plan"] is not None
        assert len(d["results"]) == 1
        assert d["milestones_completed"] == 1
        assert d["milestones_total"] == 1
        assert d["success_rate"] == pytest.approx(1.0)
        assert d["total_cost_usd"] == round(1.23456789, 4)
        assert d["total_time_seconds"] == round(99.999, 1)

    def test_to_dict_no_plan(self):
        report = ProjectReport(project_id="p1", request="X", preset="default")
        d = report.to_dict()
        assert d["plan"] is None
        assert d["milestones_total"] == 0

    def test_to_dict_empty_results(self):
        report = ProjectReport(
            project_id="p1",
            request="X",
            preset="default",
            plan=ProjectPlan(summary="Y"),
        )
        d = report.to_dict()
        assert d["results"] == []
        assert d["milestones_completed"] == 0
        assert d["success_rate"] == 0.0


# ===================================================================
# 5. ProjectRunner.__init__()
# ===================================================================


class TestProjectRunnerInit:
    def test_defaults(self, tmp_path: Path):
        config = _make_config()
        runner = ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="test-proj",
        )
        assert runner.max_milestones == DEFAULT_MAX_MILESTONES
        assert runner.max_milestones == 10
        assert runner.max_iterations == 5
        assert runner.max_cost_per_milestone == 5.0
        assert runner.max_total_cost == 50.0
        assert runner.max_wall_time == 120
        assert runner.step_callback is None
        assert runner.artifacts_dir == tmp_path / "runs"

    def test_custom_values(self, tmp_path: Path):
        config = _make_config()
        cb = MagicMock()
        runner = ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="custom",
            artifacts_dir=tmp_path / "custom-artifacts",
            max_milestones=3,
            max_iterations_per_milestone=2,
            max_cost_per_milestone=1.0,
            max_total_cost=5.0,
            max_wall_time_minutes=10.0,
            step_callback=cb,
        )
        assert runner.max_milestones == 3
        assert runner.max_iterations == 2
        assert runner.max_cost_per_milestone == 1.0
        assert runner.max_total_cost == 5.0
        assert runner.max_wall_time == 10.0
        assert runner.step_callback is cb
        assert runner.artifacts_dir == tmp_path / "custom-artifacts"


# ===================================================================
# 6. ProjectRunner._get_preset_name()
# ===================================================================


class TestGetPresetName:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("claude-sonnet-4-20250514", "claude"),
            ("claude-opus-4-6", "claude"),
            ("gpt-4.1", "openai"),
            ("gpt-4o-mini", "openai"),
            ("gemini-2.5-pro", "google"),
            ("gemini-2.5-flash", "google"),
            ("qwen2.5-coder:14b", "ollama"),
            ("qwen3:8b", "ollama"),
            ("deepseek-r1:14b", "ollama"),
            ("llama-3.1-70b", "custom"),
            ("some-unknown-model", "custom"),
        ],
    )
    def test_preset_name(self, model: str, expected: str, tmp_path: Path):
        config = _make_config(architect_model=model)
        runner = ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="test",
        )
        assert runner._get_preset_name() == expected


# ===================================================================
# 7. ProjectRunner.execute()
# ===================================================================


class TestProjectRunnerExecute:
    def _make_runner(
        self,
        tmp_path: Path,
        max_total_cost: float = 50.0,
        max_wall_time_minutes: float = 120.0,
    ) -> ProjectRunner:
        config = _make_config()
        return ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="proj-test",
            artifacts_dir=tmp_path / "artifacts",
            max_total_cost=max_total_cost,
            max_wall_time_minutes=max_wall_time_minutes,
        )

    def _plan_with_milestones(self, count: int) -> ProjectPlan:
        return ProjectPlan(
            summary=f"Project with {count} milestones",
            milestones=[
                _make_milestone(id=f"m{i + 1}", title=f"MS-{i + 1}", order=i + 1)
                for i in range(count)
            ],
            total_estimated_iterations=count * 5,
        )

    @pytest.mark.asyncio
    async def test_happy_path_all_approved(self, tmp_path: Path):
        runner = self._make_runner(tmp_path)
        plan = self._plan_with_milestones(3)

        async def mock_run_milestone(milestone, index, store, **kwargs):
            return _make_result(
                milestone_id=milestone.id,
                title=milestone.title,
                status="approved",
                cost=0.10,
            )

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            report = await runner.execute("Build an app")

        assert report.status == "completed"
        assert len(report.results) == 3
        assert report.milestones_completed == 3
        assert report.milestones_total == 3
        assert report.success_rate == pytest.approx(1.0)
        assert report.total_cost_usd == pytest.approx(0.30)
        assert report.error == ""
        assert report.started_at != ""
        assert report.completed_at != ""

    @pytest.mark.asyncio
    async def test_partial_completion(self, tmp_path: Path):
        runner = self._make_runner(tmp_path)
        plan = self._plan_with_milestones(3)

        statuses = ["approved", "approved", "exhausted"]

        async def mock_run_milestone(milestone, index, store, **kwargs):
            return _make_result(
                milestone_id=milestone.id,
                title=milestone.title,
                status=statuses[index],
                cost=0.10,
            )

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            report = await runner.execute("Build an app")

        assert report.status == "completed_partial"
        assert report.milestones_completed == 2
        assert len(report.results) == 3

    @pytest.mark.asyncio
    async def test_error_stops_early(self, tmp_path: Path):
        runner = self._make_runner(tmp_path)
        plan = self._plan_with_milestones(3)

        async def mock_run_milestone(milestone, index, store, **kwargs):
            if index == 1:
                return MilestoneResult(
                    milestone_id=milestone.id,
                    title=milestone.title,
                    status="error",
                    iterations=0,
                    cost_usd=0.0,
                    elapsed_seconds=1.0,
                    emcs_score=0.0,
                    error="Docker crashed",
                )
            return _make_result(
                milestone_id=milestone.id,
                title=milestone.title,
                status="approved",
                cost=0.10,
            )

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            report = await runner.execute("Build an app")

        assert report.status == "failed"
        assert "Docker crashed" in report.error
        # Only 2 milestones ran (m1 approved, m2 error, m3 skipped)
        assert len(report.results) == 2

    @pytest.mark.asyncio
    async def test_cost_limit_stops_early(self, tmp_path: Path):
        runner = self._make_runner(tmp_path, max_total_cost=0.15)
        plan = self._plan_with_milestones(3)

        async def mock_run_milestone(milestone, index, store, **kwargs):
            return _make_result(
                milestone_id=milestone.id,
                title=milestone.title,
                status="approved",
                cost=0.10,
            )

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            report = await runner.execute("Build an app")

        # After milestone 1 costs $0.10, milestone 2 costs $0.10 => total $0.20 >= $0.15
        # The cost check happens BEFORE running each milestone, so:
        # - m1 runs (total_cost=0 < 0.15) -> cost becomes 0.10
        # - m2 runs (total_cost=0.10 < 0.15) -> cost becomes 0.20
        # - m3 check (total_cost=0.20 >= 0.15) -> stops
        assert report.status == "failed"
        assert "cost budget exceeded" in report.error.lower()
        assert len(report.results) == 2

    @pytest.mark.asyncio
    async def test_wall_time_stops_early(self, tmp_path: Path):
        runner = self._make_runner(tmp_path, max_wall_time_minutes=0.0)
        plan = self._plan_with_milestones(3)

        # With max_wall_time=0, any elapsed time will exceed it.
        # The wall time check uses time.monotonic() which will always be > 0
        # relative to project_start. We need monotonic to advance, which it will
        # naturally.

        async def mock_run_milestone(milestone, index, store, **kwargs):
            return _make_result(
                milestone_id=milestone.id,
                title=milestone.title,
                status="approved",
                cost=0.01,
            )

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            # Patch time.monotonic to simulate elapsed time
            call_count = 0

            def fake_monotonic():
                nonlocal call_count
                call_count += 1
                # First call sets project_start, subsequent calls advance time
                if call_count == 1:
                    return 0.0
                return 600.0  # 10 minutes elapsed

            with patch("delegatron.project.time.monotonic", side_effect=fake_monotonic):
                report = await runner.execute("Build an app")

        assert report.status == "failed"
        assert "wall time" in report.error.lower()

    @pytest.mark.asyncio
    async def test_planning_failure(self, tmp_path: Path):
        runner = self._make_runner(tmp_path)

        with patch.object(
            runner,
            "_create_project_plan",
            new_callable=AsyncMock,
            side_effect=ValueError("LLM returned garbage"),
        ):
            report = await runner.execute("Build an app")

        assert report.status == "failed"
        assert "planning failed" in report.error.lower()
        assert report.plan is None
        assert report.results == []

    @pytest.mark.asyncio
    async def test_preset_in_report(self, tmp_path: Path):
        config = Config(
            architect=RoleConfig(provider="mock", model="gemini-2.5-pro"),
            implementer=RoleConfig(provider="mock", model="mock"),
            reviewer=RoleConfig(provider="mock", model="mock"),
        )
        runner = ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="test",
            artifacts_dir=tmp_path / "art",
        )
        plan = ProjectPlan(summary="X", milestones=[])

        with patch.object(
            runner,
            "_create_project_plan",
            new_callable=AsyncMock,
            return_value=plan,
        ):
            report = await runner.execute("Build it")

        assert report.preset == "google"
        assert report.status == "completed"

    @pytest.mark.asyncio
    async def test_step_callback_is_called(self, tmp_path: Path):
        config = _make_config()
        callback = MagicMock()
        runner = ProjectRunner(
            config=config,
            workspace_root=tmp_path,
            project_id="cb-test",
            artifacts_dir=tmp_path / "art",
            step_callback=callback,
        )
        plan = self._plan_with_milestones(1)

        async def mock_run_milestone(milestone, index, store, **kwargs):
            return _make_result(status="approved", cost=0.01)

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=mock_run_milestone),
        ):
            await runner.execute("Build it")

        # Callback should have been called for project_start, project_planned,
        # milestone_start, milestone_complete, project_complete
        event_names = [call.args[0] for call in callback.call_args_list]
        assert "project_start" in event_names
        assert "project_planned" in event_names
        assert "milestone_start" in event_names
        assert "milestone_complete" in event_names
        assert "project_complete" in event_names


class TestRunMilestonePipelineWiring:
    """_run_milestone reports every changed file and chains milestones (#120)."""

    @pytest.mark.parametrize(("index", "allow_dirty"), [(0, False), (1, True)])
    async def test_files_created_come_from_the_file_history(
        self, tmp_path: Path, index: int, allow_dirty: bool
    ) -> None:
        captured: dict = {}

        class _FakePipeline:
            def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
                captured.update(kwargs)

            async def execute(self, request: str, stream: bool = False) -> dict:
                return {
                    "status": "approved",
                    "iteration": 3,
                    "total_cost_usd": 0.0,
                    "current_patch": {"files": {"tests/test_b.py": "..."}},
                    "_file_history": {
                        "a.py": {"task_id": "t1", "iteration": 1},
                        "tests/test_b.py": {"task_id": "t2", "iteration": 3},
                    },
                }

        runner = ProjectRunner(config=_make_config(), workspace_root=tmp_path, project_id="p1")
        store = MagicMock()
        with patch("delegatron.project.Pipeline", _FakePipeline):
            result = await runner._run_milestone(_make_milestone(), index, store)

        assert result.files_created == ["a.py", "tests/test_b.py"]
        assert captured["allow_dirty"] is allow_dirty

    async def test_milestone_result_reports_task_outcomes(self, tmp_path: Path) -> None:
        tasks = [
            {"task_id": "t1", "title": "Card"},
            {"task_id": "t2", "title": "Deal"},
        ]

        class _FakePipeline:
            def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
                pass

            async def execute(self, request: str, stream: bool = False) -> dict:
                return {
                    "status": "partial",
                    "iteration": 6,
                    "total_cost_usd": 0.0,
                    "all_tasks": tasks,
                    "_task_outcomes": {
                        "t2": {"status": "approved", "iterations": 1},
                        "t1": {"status": "exhausted", "iterations": 5},
                    },
                }

        runner = ProjectRunner(config=_make_config(), workspace_root=tmp_path, project_id="p1")
        with patch("delegatron.project.Pipeline", _FakePipeline):
            result = await runner._run_milestone(_make_milestone(), 0, MagicMock())

        assert result.status == "partial"
        assert result.to_dict()["task_outcomes"] == [
            {"task_id": "t1", "title": "Card", "status": "exhausted", "iterations": 5},
            {"task_id": "t2", "title": "Deal", "status": "approved", "iterations": 1},
        ]


class TestPartialMilestone:
    async def test_partial_milestone_makes_the_project_partial(self, tmp_path: Path) -> None:
        runner = ProjectRunner(
            config=_make_config(), workspace_root=tmp_path, project_id="p1", artifacts_dir=tmp_path
        )
        plan = ProjectPlan(summary="s", milestones=[_make_milestone(id="m1")])

        async def run(milestone, index, store, **kwargs):  # type: ignore[no-untyped-def]
            return _make_result(milestone_id=milestone.id, status="partial")

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=run),
        ):
            report = await runner.execute("Build an app")

        assert report.status == "completed_partial"
        assert report.milestones_completed == 0


class TestProjectPlanSampling:
    async def test_meta_architect_uses_the_architect_role_sampling(self, tmp_path: Path) -> None:
        """#76: the planning call honours the architect's temperature and max_tokens."""
        from delegatron.agents.providers.base import ProviderResponse

        config = Config(
            architect=RoleConfig(
                provider="mock", model="mock-arch", temperature=0.35, max_tokens=4321
            ),
            implementer=RoleConfig(provider="mock", model="mock-impl"),
            reviewer=RoleConfig(provider="mock", model="mock-rev"),
        )
        calls: list[dict] = []

        class _RecordingProvider:
            async def generate(self, **kwargs) -> ProviderResponse:  # type: ignore[no-untyped-def]
                calls.append(kwargs)
                return ProviderResponse(
                    content='{"summary": "s", "milestones": [{"id": "m1", "title": "T", '
                    '"description": "d"}]}',
                    model="mock-arch",
                )

        runner = ProjectRunner(config=config, workspace_root=tmp_path, project_id="p")
        with patch(
            "delegatron.agents.providers.registry.get_provider",
            return_value=_RecordingProvider(),
        ):
            plan = await runner._create_project_plan("Build a CLI")

        assert [m.id for m in plan.milestones] == ["m1"]
        assert calls[0]["temperature"] == 0.35
        assert calls[0]["max_tokens"] == 4321
        # The prompt is an external, versioned file (design rule 7)
        from delegatron.prompts import load_prompt

        assert calls[0]["system_prompt"] == load_prompt("meta_architect", version=1)
        assert "sequential milestones" in calls[0]["system_prompt"]


class TestMilestoneChaining:
    async def test_next_milestone_forks_from_the_branch_the_last_one_used(
        self, tmp_path: Path
    ) -> None:
        runner = ProjectRunner(
            config=_make_config(),
            workspace_root=tmp_path,
            project_id="p1",
            artifacts_dir=tmp_path / "art",
            base_branch="develop",
        )
        plan = ProjectPlan(
            summary="s", milestones=[_make_milestone(id="m1"), _make_milestone(id="m2")]
        )
        bases: list[str] = []

        async def run(milestone, index, store, base_branch=""):  # type: ignore[no-untyped-def]
            bases.append(base_branch)
            result = _make_result(milestone_id=milestone.id)
            result.branch = f"delegatron/run-p1-m{index + 1}-2"
            return result

        with (
            patch.object(runner, "_create_project_plan", new_callable=AsyncMock, return_value=plan),
            patch.object(runner, "_run_milestone", side_effect=run),
        ):
            await runner.execute("Build an app")

        assert bases == ["develop", "delegatron/run-p1-m1-2"]

    async def test_milestone_reports_its_branch_deletions_and_dirty_opt_in(
        self, tmp_path: Path
    ) -> None:
        captured: dict = {}

        class _FakePipeline:
            def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
                captured.update(kwargs)

            async def execute(self, request: str, stream: bool = False) -> dict:
                return {
                    "status": "approved",
                    "iteration": 1,
                    "total_cost_usd": 0.0,
                    "messages": [
                        {"event": "workspace_initialized", "branch": "delegatron/run-p1-m1"}
                    ],
                    "_file_history": {
                        "new.py": {"task_id": "t1", "iteration": 1},
                        "old.py": {"task_id": "t1", "iteration": 1, "deleted": True},
                    },
                }

        runner = ProjectRunner(
            config=_make_config(), workspace_root=tmp_path, project_id="p1", allow_dirty=True
        )
        with patch("delegatron.project.Pipeline", _FakePipeline):
            result = await runner._run_milestone(_make_milestone(), 0, MagicMock())

        assert result.branch == "delegatron/run-p1-m1"
        assert result.files_created == ["new.py"]
        assert result.files_deleted == ["old.py"]
        assert captured["allow_dirty"] is True
        assert captured["base_branch"] == ""
