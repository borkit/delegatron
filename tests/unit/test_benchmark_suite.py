"""Tests for the regression benchmark suite module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from delegatron.benchmark_suite import (
    BUILTIN_CASES,
    BenchmarkCase,
    BenchmarkSuite,
    CaseResult,
    SuiteReport,
)

# ---------------------------------------------------------------------------
# Dataclass basics
# ---------------------------------------------------------------------------


class TestBenchmarkCase:
    def test_defaults(self) -> None:
        case = BenchmarkCase(name="t", request="do stuff")
        assert case.expected_files == []
        assert case.expected_tests_pass is True
        assert case.max_iterations == 5
        assert case.max_cost_usd == 2.0

    def test_custom_values(self) -> None:
        case = BenchmarkCase(
            name="c",
            request="r",
            expected_files=["a.py"],
            expected_tests_pass=False,
            max_iterations=10,
            max_cost_usd=5.0,
        )
        assert case.expected_files == ["a.py"]
        assert case.expected_tests_pass is False


class TestCaseResult:
    def test_defaults(self) -> None:
        r = CaseResult(case_name="x", passed=True)
        assert r.iterations == 0
        assert r.cost == 0.0
        assert r.time == 0.0
        assert r.error is None

    def test_with_error(self) -> None:
        r = CaseResult(case_name="x", passed=False, error="boom")
        assert r.error == "boom"


class TestSuiteReport:
    def test_defaults(self) -> None:
        report = SuiteReport()
        assert report.cases == []
        assert report.total_time == 0.0
        assert report.total_cost == 0.0
        assert report.pass_rate == 0.0


# ---------------------------------------------------------------------------
# Built-in cases
# ---------------------------------------------------------------------------


def test_builtin_cases_not_empty() -> None:
    assert len(BUILTIN_CASES) >= 3


def test_builtin_cases_have_names() -> None:
    names = [c.name for c in BUILTIN_CASES]
    assert "hello_world" in names
    assert "crud_endpoint" in names
    assert "bug_fix" in names


# ---------------------------------------------------------------------------
# BenchmarkSuite
# ---------------------------------------------------------------------------


class TestBenchmarkSuiteInit:
    def test_defaults_to_builtin(self) -> None:
        suite = BenchmarkSuite()
        assert len(suite.cases) == len(BUILTIN_CASES)

    def test_custom_cases(self) -> None:
        cases = [BenchmarkCase(name="a", request="b")]
        suite = BenchmarkSuite(cases=cases)
        assert suite.cases == cases

    def test_default_workspace(self) -> None:
        suite = BenchmarkSuite()
        assert suite.workspace_root == Path(".")

    def test_custom_workspace(self, tmp_path: Path) -> None:
        suite = BenchmarkSuite(workspace_root=tmp_path)
        assert suite.workspace_root == tmp_path


class TestMockRun:
    @pytest.mark.asyncio
    async def test_run_case_mock_preset(self) -> None:
        case = BenchmarkCase(name="test_mock", request="do something")
        suite = BenchmarkSuite(cases=[case])
        result = await suite.run_case(case, preset="mock")

        assert result.passed is True
        assert result.case_name == "test_mock"
        assert result.iterations == 1
        assert result.cost == 0.0
        assert result.time >= 0.0

    @pytest.mark.asyncio
    async def test_run_all_mock(self) -> None:
        cases = [
            BenchmarkCase(name="a", request="r1"),
            BenchmarkCase(name="b", request="r2"),
        ]
        suite = BenchmarkSuite(cases=cases)
        report = await suite.run_all(preset="mock")

        assert len(report.cases) == 2
        assert all(c.passed for c in report.cases)
        assert report.pass_rate == 1.0
        assert report.total_time >= 0.0
        assert report.total_cost == 0.0

    @pytest.mark.asyncio
    async def test_run_all_empty_cases(self) -> None:
        suite = BenchmarkSuite(cases=[])
        report = await suite.run_all(preset="mock")

        assert report.cases == []
        assert report.pass_rate == 0.0


class TestRunCaseException:
    @pytest.mark.asyncio
    async def test_exception_returns_failed_result(self) -> None:
        case = BenchmarkCase(name="boom", request="x")
        suite = BenchmarkSuite(cases=[case])

        with patch.object(suite, "_mock_run", side_effect=RuntimeError("kaboom")):
            result = await suite.run_case(case, preset="mock")

        assert result.passed is False
        assert result.case_name == "boom"
        assert result.error == "kaboom"
        assert result.time >= 0.0


class TestLiveRun:
    @pytest.mark.asyncio
    async def test_live_run_success(self, tmp_path: Path) -> None:
        case = BenchmarkCase(
            name="live_test",
            request="do stuff",
            expected_files=["out.py"],
            expected_tests_pass=True,
        )
        # Create expected file
        (tmp_path / "out.py").write_text("pass")

        suite = BenchmarkSuite(cases=[case], workspace_root=tmp_path)

        mock_result = {
            "iteration": 2,
            "total_cost_usd": 0.5,
            "test_report": {"passed": True},
        }

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.execute = AsyncMock(return_value=mock_result)

        with (
            patch("delegatron.config.load_config", return_value=MagicMock()),
            patch(
                "delegatron.orchestrator.graph.Pipeline",
                return_value=mock_pipeline_instance,
            ),
        ):
            import time

            start = time.monotonic()
            result = await suite._live_run(case, "default", start)

        assert result.passed is True
        assert result.iterations == 2
        assert result.cost == 0.5

    @pytest.mark.asyncio
    async def test_live_run_missing_files(self, tmp_path: Path) -> None:
        case = BenchmarkCase(
            name="live_missing",
            request="do stuff",
            expected_files=["missing.py"],
            expected_tests_pass=True,
        )
        suite = BenchmarkSuite(cases=[case], workspace_root=tmp_path)

        mock_result = {
            "iteration": 1,
            "total_cost_usd": 0.1,
            "test_report": {"passed": True},
        }

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.execute = AsyncMock(return_value=mock_result)

        with (
            patch("delegatron.config.load_config", return_value=MagicMock()),
            patch(
                "delegatron.orchestrator.graph.Pipeline",
                return_value=mock_pipeline_instance,
            ),
        ):
            import time

            start = time.monotonic()
            result = await suite._live_run(case, "default", start)

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_live_run_tests_failed(self, tmp_path: Path) -> None:
        case = BenchmarkCase(
            name="live_fail",
            request="do stuff",
            expected_files=[],
            expected_tests_pass=True,
        )
        suite = BenchmarkSuite(cases=[case], workspace_root=tmp_path)

        mock_result = {
            "iteration": 1,
            "total_cost_usd": 0.1,
            "test_report": {"passed": False},
        }

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.execute = AsyncMock(return_value=mock_result)

        with (
            patch("delegatron.config.load_config", return_value=MagicMock()),
            patch(
                "delegatron.orchestrator.graph.Pipeline",
                return_value=mock_pipeline_instance,
            ),
        ):
            import time

            start = time.monotonic()
            result = await suite._live_run(case, "default", start)

        assert result.passed is False


class TestLiveRunWiring:
    @pytest.mark.asyncio
    async def test_live_cases_get_fresh_run_ids_one_base_and_the_dirty_opt_in(
        self, tmp_path: Path
    ) -> None:
        cases = [BenchmarkCase(name="c", request="r"), BenchmarkCase(name="c", request="r")]
        suite = BenchmarkSuite(
            cases=cases, workspace_root=tmp_path, allow_dirty=True, base_branch="trunk"
        )
        pipeline_instance = MagicMock()
        pipeline_instance.execute = AsyncMock(return_value={"test_report": {"passed": True}})

        with (
            patch("delegatron.config.load_config", return_value=MagicMock()),
            patch(
                "delegatron.orchestrator.graph.Pipeline", return_value=pipeline_instance
            ) as pipeline_cls,
        ):
            for case in cases:
                await suite._live_run(case, "default", 0.0)

        calls = [call.kwargs for call in pipeline_cls.call_args_list]
        assert calls[0]["run_id"] != calls[1]["run_id"]
        assert all(kw["run_id"].startswith("bench-c-") for kw in calls)
        assert [kw["base_branch"] for kw in calls] == ["trunk", "trunk"]
        assert [kw["allow_dirty"] for kw in calls] == [True, True]
