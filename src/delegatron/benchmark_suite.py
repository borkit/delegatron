"""Regression benchmark suite for validating pipeline quality over time."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkCase:
    """A single benchmark scenario to execute."""

    name: str
    request: str
    expected_files: list[str] = field(default_factory=list)
    expected_tests_pass: bool = True
    max_iterations: int = 5
    max_cost_usd: float = 2.0


@dataclass
class CaseResult:
    """Outcome of a single benchmark case execution."""

    case_name: str
    passed: bool
    iterations: int = 0
    cost: float = 0.0
    time: float = 0.0
    error: str | None = None


@dataclass
class SuiteReport:
    """Aggregated results from running the full benchmark suite."""

    cases: list[CaseResult] = field(default_factory=list)
    total_time: float = 0.0
    total_cost: float = 0.0
    pass_rate: float = 0.0


# Built-in benchmark cases
BUILTIN_CASES: list[BenchmarkCase] = [
    BenchmarkCase(
        name="hello_world",
        request="Create a Python script that prints 'Hello, World!' and a test for it.",
        expected_files=["hello.py", "test_hello.py"],
        expected_tests_pass=True,
        max_iterations=3,
        max_cost_usd=1.0,
    ),
    BenchmarkCase(
        name="crud_endpoint",
        request=(
            "Create a FastAPI CRUD endpoint for a 'Task' model with "
            "fields: id (int), title (str), done (bool). Include tests."
        ),
        expected_files=["tasks.py", "test_tasks.py"],
        expected_tests_pass=True,
        max_iterations=5,
        max_cost_usd=3.0,
    ),
    BenchmarkCase(
        name="bug_fix",
        request=(
            "Fix the off-by-one error in the function `def paginate(items, page, size): "
            "return items[page*size:(page+1)*size]` so that page numbering starts at 1."
        ),
        expected_files=["paginate.py"],
        expected_tests_pass=True,
        max_iterations=3,
        max_cost_usd=1.0,
    ),
]


class BenchmarkSuite:
    """Runs a collection of benchmark cases and produces a report."""

    def __init__(
        self,
        cases: list[BenchmarkCase] | None = None,
        workspace_root: Path | None = None,
        *,
        allow_dirty: bool = False,
        base_branch: str | None = None,
    ) -> None:
        self.cases = cases if cases is not None else list(BUILTIN_CASES)
        self.workspace_root = workspace_root or Path(".")
        # Start live runs even when the git workspace has uncommitted changes.
        self.allow_dirty = allow_dirty
        # Branch every live case forks from (None = the branch checked out at
        # the first case), so cases never build on each other's output.
        self.base_branch = base_branch

    async def run_all(self, preset: str = "mock") -> SuiteReport:
        """Execute every benchmark case and return the aggregated report."""
        suite_start = time.monotonic()
        results: list[CaseResult] = []

        for case in self.cases:
            result = await self.run_case(case, preset)
            results.append(result)

        total_time = time.monotonic() - suite_start
        total_cost = sum(r.cost for r in results)
        passed = sum(1 for r in results if r.passed)
        pass_rate = passed / len(results) if results else 0.0

        return SuiteReport(
            cases=results,
            total_time=total_time,
            total_cost=total_cost,
            pass_rate=pass_rate,
        )

    async def run_case(self, case: BenchmarkCase, preset: str = "mock") -> CaseResult:
        """Execute a single benchmark case.

        Uses the orchestrator pipeline when available, or returns a mock
        result for the 'mock' preset to enable offline testing.
        """
        case_start = time.monotonic()

        try:
            if preset == "mock":
                return self._mock_run(case, case_start)

            return await self._live_run(case, preset, case_start)

        except Exception as exc:
            elapsed = time.monotonic() - case_start
            logger.exception("Benchmark case '%s' failed", case.name)
            return CaseResult(
                case_name=case.name,
                passed=False,
                time=elapsed,
                error=str(exc),
            )

    def _mock_run(self, case: BenchmarkCase, start: float) -> CaseResult:
        """Return a synthetic passing result for offline/CI testing."""
        elapsed = time.monotonic() - start
        return CaseResult(
            case_name=case.name,
            passed=True,
            iterations=1,
            cost=0.0,
            time=elapsed,
        )

    async def _live_run(self, case: BenchmarkCase, preset: str, start: float) -> CaseResult:
        """Run the case through the real pipeline."""
        from delegatron.benchmark import benchmark_run_id
        from delegatron.config import load_config
        from delegatron.orchestrator.graph import Pipeline
        from delegatron.workspace import Workspace

        config = load_config(preset=preset)
        if self.base_branch is None:
            self.base_branch = Workspace.checked_out_branch(self.workspace_root)
        pipeline = Pipeline(
            config=config,
            workspace_root=self.workspace_root,
            run_id=benchmark_run_id(case.name),
            base_branch=self.base_branch,
            allow_dirty=self.allow_dirty,
        )

        result = await pipeline.execute(case.request)
        state: dict[str, Any] = dict(result)

        elapsed = time.monotonic() - start
        iterations = state.get("iteration", 0)
        cost = state.get("total_cost_usd", 0.0)

        # Verify expected files exist
        files_ok = all((self.workspace_root / f).exists() for f in case.expected_files)
        test_report = state.get("test_report") or {}
        tests_passed = test_report.get("passed", False)
        passed = files_ok and (tests_passed if case.expected_tests_pass else True)

        return CaseResult(
            case_name=case.name,
            passed=passed,
            iterations=iterations,
            cost=cost,
            time=elapsed,
        )
