"""Quality benchmark — compare pipeline configurations empirically.

Runs the same task with different presets/configs and compares:
- EMCS quality score
- Iteration count
- Total cost
- Wall-clock time

Results are saved as a benchmark report for analysis.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegatron.config import load_config
from delegatron.orchestrator.graph import Pipeline
from delegatron.orchestrator.scoring import compute_score
from delegatron.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    preset: str
    status: str
    iterations: int
    cost_usd: float
    elapsed_seconds: float
    emcs_score: float
    approved: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "status": self.status,
            "iterations": self.iterations,
            "cost_usd": self.cost_usd,
            "elapsed_seconds": self.elapsed_seconds,
            "emcs_score": self.emcs_score,
            "approved": self.approved,
            "error": self.error,
        }


@dataclass
class BenchmarkReport:
    """Aggregated benchmark results."""

    request: str
    results: list[BenchmarkResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "results": [r.to_dict() for r in self.results],
            "best_quality": self._best_by("emcs_score"),
            "cheapest": self._cheapest(),
            "fastest": self._fastest(),
        }

    def _best_by(self, key: str) -> str | None:
        successful = [r for r in self.results if r.approved]
        if not successful:
            return None
        return max(successful, key=lambda r: getattr(r, key)).preset

    def _cheapest(self) -> str | None:
        successful = [r for r in self.results if r.approved]
        if not successful:
            return None
        return min(successful, key=lambda r: r.cost_usd).preset

    def _fastest(self) -> str | None:
        successful = [r for r in self.results if r.approved]
        if not successful:
            return None
        return min(successful, key=lambda r: r.elapsed_seconds).preset


def benchmark_run_id(name: str) -> str:
    """A run id for one benchmark run of *name* that no earlier run used.

    A reused id would resume that run's branch, so the run would start from
    the code an earlier benchmark generated.
    """
    return f"bench-{name}-{uuid.uuid4().hex[:8]}"


async def run_benchmark(
    request: str,
    workspace_root: Path,
    presets: list[str],
    *,
    allow_dirty: bool = False,
    base_branch: str | None = None,
) -> BenchmarkReport:
    """Run the same task with multiple presets and compare results.

    Every preset forks from the same base: *base_branch*, else the branch
    checked out when the benchmark starts.  *allow_dirty* lets runs start on a
    workspace with uncommitted changes (e.g. byproducts of an earlier preset).
    """
    report = BenchmarkReport(request=request)
    base = Workspace.checked_out_branch(workspace_root) if base_branch is None else base_branch

    for preset in presets:
        logger.info("Benchmark: running preset '%s'...", preset)
        start = time.monotonic()

        try:
            config = load_config(preset=preset)
            run_id = benchmark_run_id(preset)

            pipeline = Pipeline(
                config=config,
                workspace_root=workspace_root,
                run_id=run_id,
                base_branch=base,
                allow_dirty=allow_dirty,
            )

            state = await pipeline.execute(request)
            elapsed = time.monotonic() - start

            # Compute EMCS score from final review
            review = state.get("review_report")
            test_report = state.get("test_report")
            score = compute_score(review or {}, test_report) if review is not None else 0.0

            result = BenchmarkResult(
                preset=preset,
                status=state.get("status", "unknown"),
                iterations=state.get("iteration", 0),
                cost_usd=state.get("total_cost_usd", 0.0),
                elapsed_seconds=round(elapsed, 1),
                emcs_score=score,
                approved=state.get("is_approved", False),
            )

        except Exception as e:
            elapsed = time.monotonic() - start
            result = BenchmarkResult(
                preset=preset,
                status="error",
                iterations=0,
                cost_usd=0.0,
                elapsed_seconds=round(elapsed, 1),
                emcs_score=0.0,
                approved=False,
                error=str(e),
            )

        report.results.append(result)
        logger.info(
            "Benchmark '%s': %s (score=%.2f, cost=$%.4f, %ds)",
            preset,
            result.status,
            result.emcs_score,
            result.cost_usd,
            result.elapsed_seconds,
        )

    return report
