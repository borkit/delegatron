"""SWE-bench adapter — evaluate Delegatron on real GitHub issues.

Loads SWE-bench instances (from HuggingFace or local JSONL), feeds them
through the pipeline, and evaluates results by checking if the generated
patch makes the FAIL_TO_PASS tests pass while keeping PASS_TO_PASS green.

Usage:
    # CLI
    delegatron swebench --preset local --instances 10
    delegatron swebench --preset default --split verified --instances 50
    delegatron swebench compare results_multi.json results_single.json

    # Python
    adapter = SWEBenchAdapter(preset="local")
    report = await adapter.run(limit=10)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from delegatron.config import load_config
from delegatron.orchestrator.graph import Pipeline
from delegatron.orchestrator.scoring import compute_score

logger = logging.getLogger(__name__)

# SWE-bench HuggingFace dataset IDs
DATASETS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}

# Complexity tiers based on patch size (lines changed in gold patch)
TIER_THRESHOLDS = {
    "simple": 10,  # 1-10 lines changed
    "moderate": 50,  # 11-50 lines changed
    "complex": 200,  # 51-200 lines changed
    "feature": 99999,  # 200+ lines changed
}


@dataclass
class SWEBenchInstance:
    """A single SWE-bench task."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str  # gold patch (for tier classification only)
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    version: str = ""
    hints_text: str = ""

    @property
    def tier(self) -> str:
        """Classify instance complexity by gold patch size."""
        lines = len(
            [
                ln
                for ln in self.patch.split("\n")
                if (ln.startswith("+") or ln.startswith("-"))
                and not ln.startswith("+++")
                and not ln.startswith("---")
            ]
        )
        for tier_name, threshold in TIER_THRESHOLDS.items():
            if lines <= threshold:
                return tier_name
        return "feature"

    @property
    def patch_lines(self) -> int:
        """Count of changed lines in the gold patch."""
        return len(
            [
                ln
                for ln in self.patch.split("\n")
                if (ln.startswith("+") or ln.startswith("-"))
                and not ln.startswith("+++")
                and not ln.startswith("---")
            ]
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SWEBenchInstance:
        fail_to_pass = d.get("FAIL_TO_PASS", "[]")
        pass_to_pass = d.get("PASS_TO_PASS", "[]")
        if isinstance(fail_to_pass, str):
            fail_to_pass = json.loads(fail_to_pass)
        if isinstance(pass_to_pass, str):
            pass_to_pass = json.loads(pass_to_pass)
        return cls(
            instance_id=d["instance_id"],
            repo=d["repo"],
            base_commit=d["base_commit"],
            problem_statement=d["problem_statement"],
            hints_text=d.get("hints_text", ""),
            patch=d.get("patch", ""),
            test_patch=d.get("test_patch", ""),
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            version=d.get("version", ""),
        )


@dataclass
class SWEBenchResult:
    """Result of running Delegatron on one SWE-bench instance."""

    instance_id: str
    tier: str
    resolved: bool
    status: str
    iterations: int
    cost_usd: float
    elapsed_seconds: float
    emcs_score: float
    patch_generated: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "tier": self.tier,
            "resolved": self.resolved,
            "status": self.status,
            "iterations": self.iterations,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "emcs_score": round(self.emcs_score, 3),
            "error": self.error,
        }


@dataclass
class SWEBenchReport:
    """Aggregated SWE-bench evaluation report."""

    preset: str
    split: str
    total_instances: int
    results: list[SWEBenchResult] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""

    @property
    def resolve_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.resolved) / len(self.results)

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def total_time(self) -> float:
        return sum(r.elapsed_seconds for r in self.results)

    @property
    def avg_iterations(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.iterations for r in self.results) / len(self.results)

    def by_tier(self) -> dict[str, dict[str, Any]]:
        """Break down results by complexity tier."""
        tiers: dict[str, list[SWEBenchResult]] = {}
        for r in self.results:
            tiers.setdefault(r.tier, []).append(r)

        breakdown: dict[str, dict[str, Any]] = {}
        for tier_name, tier_results in sorted(tiers.items()):
            resolved = sum(1 for r in tier_results if r.resolved)
            breakdown[tier_name] = {
                "total": len(tier_results),
                "resolved": resolved,
                "resolve_rate": round(resolved / len(tier_results), 3) if tier_results else 0,
                "avg_cost": round(sum(r.cost_usd for r in tier_results) / len(tier_results), 4),
                "avg_iterations": round(
                    sum(r.iterations for r in tier_results) / len(tier_results), 1
                ),
            }
        return breakdown

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "split": self.split,
            "total_instances": self.total_instances,
            "completed": len(self.results),
            "resolve_rate": round(self.resolve_rate, 3),
            "total_cost_usd": round(self.total_cost, 4),
            "total_time_seconds": round(self.total_time, 1),
            "avg_iterations": round(self.avg_iterations, 1),
            "by_tier": self.by_tier(),
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def load_instances(
    split: str = "lite",
    limit: int | None = None,
    tier: str | None = None,
    jsonl_path: Path | None = None,
) -> list[SWEBenchInstance]:
    """Load SWE-bench instances from HuggingFace or a local JSONL file.

    Args:
        split: Dataset split — 'lite' (300), 'verified' (500), or 'full' (2294).
        limit: Max instances to load.
        tier: Filter by complexity tier (simple, moderate, complex, feature).
        jsonl_path: Load from local JSONL file instead of HuggingFace.
    """
    if jsonl_path:
        return _load_from_jsonl(jsonl_path, limit, tier)
    return _load_from_huggingface(split, limit, tier)


def _load_from_jsonl(path: Path, limit: int | None, tier: str | None) -> list[SWEBenchInstance]:
    """Load instances from a local JSONL file."""
    instances: list[SWEBenchInstance] = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            inst = SWEBenchInstance.from_dict(json.loads(line))
            if tier and inst.tier != tier:
                continue
            instances.append(inst)
            if limit and len(instances) >= limit:
                break
    return instances


def _load_from_huggingface(
    split: str, limit: int | None, tier: str | None
) -> list[SWEBenchInstance]:
    """Load instances from HuggingFace datasets library."""
    try:
        from datasets import load_dataset  # type: ignore[import-untyped, unused-ignore]
    except ImportError as exc:
        raise ImportError(
            "SWE-bench requires the 'datasets' package. "
            "Install with: pip install 'delegatron[swebench]'"
        ) from exc

    dataset_id = DATASETS.get(split)
    if not dataset_id:
        raise ValueError(f"Unknown split: {split!r}. Use: {list(DATASETS.keys())}")

    logger.info("Loading SWE-bench %s from HuggingFace...", split)
    ds = load_dataset(dataset_id, split="test")

    instances: list[SWEBenchInstance] = []
    for row in ds:
        inst = SWEBenchInstance.from_dict(row)
        if tier and inst.tier != tier:
            continue
        instances.append(inst)
        if limit and len(instances) >= limit:
            break

    logger.info("Loaded %d instances (split=%s, tier=%s)", len(instances), split, tier)
    return instances


def _setup_repo(instance: SWEBenchInstance, work_dir: Path) -> Path:
    """Clone the repo at the correct base commit into a working directory.

    Returns the path to the cloned repo.
    """
    repo_dir = work_dir / instance.instance_id.replace("/", "__")
    if repo_dir.exists():
        # Reset to base commit
        subprocess.run(
            ["git", "checkout", instance.base_commit, "--force"],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=repo_dir,
            capture_output=True,
            check=False,
        )
        return repo_dir

    # Clone from GitHub
    repo_url = f"https://github.com/{instance.repo}.git"
    logger.info("Cloning %s at %s...", instance.repo, instance.base_commit[:8])

    subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
        capture_output=True,
        check=True,
        timeout=120,
    )
    # Fetch the specific commit (shallow clone may not have it)
    fetch_result = subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", instance.base_commit],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if fetch_result.returncode != 0:
        # Shallow fetch failed — try full unshallow as fallback
        logger.warning(
            "Shallow fetch of %s failed, unshallowing...",
            instance.base_commit[:8],
        )
        subprocess.run(
            ["git", "fetch", "--unshallow"],
            cwd=repo_dir,
            capture_output=True,
            check=False,
            timeout=300,
        )

    checkout_result = subprocess.run(
        ["git", "checkout", instance.base_commit],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if checkout_result.returncode != 0:
        raise RuntimeError(
            f"Failed to checkout {instance.base_commit[:8]} in {instance.repo}: "
            f"{checkout_result.stderr.strip()}"
        )

    return repo_dir


def _run_diff(repo_dir: Path, base_commit: str) -> str:
    """Everything the run committed on top of *base_commit* ("" when nothing or no git).

    Covers ``files`` patches too, which leave ``diff_unified`` empty.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--binary", base_commit, "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _evaluate_patch(
    instance: SWEBenchInstance,
    generated_patch: str,
    repo_dir: Path,
) -> bool:
    """Evaluate a generated patch against the instance's test criteria.

    Applies the generated patch + the gold test patch, then runs the
    FAIL_TO_PASS tests to check resolution.

    Returns True if the patch resolves the issue.
    """
    if not generated_patch.strip():
        return False

    # Reset to base commit
    subprocess.run(
        ["git", "checkout", instance.base_commit, "--force"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=repo_dir,
        capture_output=True,
        check=False,
    )

    # Apply generated patch
    result = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=generated_patch,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.debug("Generated patch does not apply cleanly")
        return False

    apply_result = subprocess.run(
        ["git", "apply", "-"],
        input=generated_patch,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_result.returncode != 0:
        logger.warning("Generated patch failed to apply: %s", apply_result.stderr.strip())
        return False

    # Apply gold test patch (so we can run FAIL_TO_PASS tests)
    if instance.test_patch:
        test_apply = subprocess.run(
            ["git", "apply", "-"],
            input=instance.test_patch,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if test_apply.returncode != 0:
            logger.warning(
                "Gold test patch failed to apply for %s: %s",
                instance.instance_id,
                test_apply.stderr.strip(),
            )
            # Continue anyway — some test patches may partially apply

    # Run FAIL_TO_PASS tests
    if not instance.fail_to_pass:
        logger.warning("No FAIL_TO_PASS tests for %s — skipping evaluation", instance.instance_id)
        return False

    # Ensure setuptools-scm doesn't block test execution on shallow clones
    test_env = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0"}
    test_cmd = ["python", "-m", "pytest", "-xvs", *instance.fail_to_pass]
    try:
        test_result = subprocess.run(
            test_cmd,
            cwd=repo_dir,
            env=test_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("FAIL_TO_PASS tests timed out for %s", instance.instance_id)
        return False

    if test_result.returncode != 0:
        logger.debug("NOT resolved: %s (rc=%d)", instance.instance_id, test_result.returncode)
        return False

    # FAIL_TO_PASS tests pass — now verify we didn't break PASS_TO_PASS tests
    if instance.pass_to_pass:
        pass_cmd = ["python", "-m", "pytest", "-xvs", *instance.pass_to_pass]
        try:
            pass_result = subprocess.run(
                pass_cmd,
                cwd=repo_dir,
                env=test_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.warning("PASS_TO_PASS tests timed out for %s", instance.instance_id)
            return False

        if pass_result.returncode != 0:
            logger.info(
                "REGRESSION: %s — FAIL_TO_PASS pass but PASS_TO_PASS fail (rc=%d)",
                instance.instance_id,
                pass_result.returncode,
            )
            return False

    logger.info("RESOLVED: %s", instance.instance_id)
    return True


class SWEBenchAdapter:
    """Runs Delegatron on SWE-bench instances and produces evaluation reports."""

    def __init__(
        self,
        preset: str = "default",
        work_dir: Path | None = None,
        artifacts_dir: Path | None = None,
        max_iterations: int = 5,
        max_cost_usd: float = 5.0,
    ) -> None:
        self.preset = preset
        self.work_dir = work_dir or Path("swebench-workdir")
        self.artifacts_dir = artifacts_dir or Path("swebench-runs")
        self.max_iterations = max_iterations
        self.max_cost_usd = max_cost_usd

    async def run(
        self,
        split: str = "lite",
        limit: int | None = None,
        tier: str | None = None,
        jsonl_path: Path | None = None,
    ) -> SWEBenchReport:
        """Run Delegatron on SWE-bench instances and evaluate results."""
        from datetime import UTC, datetime

        # Verify dependencies upfront before doing any work
        if not jsonl_path:
            try:
                import datasets  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "SWE-bench requires the 'datasets' package. "
                    "Install with: pip install 'delegatron[swebench]'"
                ) from exc

        instances = load_instances(split, limit, tier, jsonl_path)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        report = SWEBenchReport(
            preset=self.preset,
            split=split,
            total_instances=len(instances),
            started_at=datetime.now(UTC).isoformat(),
        )

        for i, instance in enumerate(instances):
            logger.info(
                "[%d/%d] %s (tier=%s, %d lines)",
                i + 1,
                len(instances),
                instance.instance_id,
                instance.tier,
                instance.patch_lines,
            )

            result = await self._run_instance(instance)
            report.results.append(result)

            # Save incremental progress
            progress_path = self.artifacts_dir / f"swebench-{self.preset}-progress.json"
            progress_path.write_text(json.dumps(report.to_dict(), indent=2))

            logger.info(
                "  %s | %s | iter=%d | cost=$%.4f | %.1fs",
                "PASS" if result.resolved else "FAIL",
                result.status,
                result.iterations,
                result.cost_usd,
                result.elapsed_seconds,
            )

        report.completed_at = datetime.now(UTC).isoformat()

        # Clean up cloned repos to reclaim disk space
        import shutil

        for child in self.work_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        logger.info("Cleaned up SWE-bench workdir: %s", self.work_dir)

        # Save final report
        report_path = self.artifacts_dir / f"swebench-{self.preset}-{split}.json"
        report_path.write_text(json.dumps(report.to_dict(), indent=2))
        logger.info(
            "SWE-bench complete: %d/%d resolved (%.1f%%) | $%.4f total | %.0fs",
            sum(1 for r in report.results if r.resolved),
            len(report.results),
            report.resolve_rate * 100,
            report.total_cost,
            report.total_time,
        )

        return report

    async def _run_instance(self, instance: SWEBenchInstance) -> SWEBenchResult:
        """Run a single SWE-bench instance through the pipeline."""
        from dataclasses import replace

        start = time.monotonic()
        generated_patch = ""
        state: dict[str, Any] | None = None

        try:
            # Setup repo at base commit
            repo_dir = _setup_repo(instance, self.work_dir)

            # Configure pipeline
            config = load_config(preset=self.preset)
            config = replace(
                config,
                pipeline=replace(
                    config.pipeline,
                    max_iterations=self.max_iterations,
                    max_cost_usd=self.max_cost_usd,
                ),
            )

            # A fresh id per attempt: a reused one would resume the branch an
            # interrupted earlier attempt left in the cached clone.
            run_id = f"swe-{instance.instance_id.replace('/', '-')}-{uuid.uuid4().hex[:8]}"

            pipeline = Pipeline(
                config=config,
                workspace_root=repo_dir,
                run_id=run_id,
                artifacts_dir=self.artifacts_dir,
                # The clone is checked out (detached) at the instance's base commit.
                base_branch=instance.base_commit,
            )

            # Simplify the problem statement for local models.
            # Raw GitHub issues contain tracebacks, code blocks, and discussion
            # that overwhelm small models. We prepend clear instructions.
            request = (
                f"Fix this bug in the {instance.repo} repository.\n\n"
                f"IMPORTANT: This is a REAL bug fix in an existing codebase. "
                f"Do NOT create a new project or new files from scratch. "
                f"Find the bug in the EXISTING source files in the workspace "
                f"and produce a minimal fix. The workspace already contains the "
                f"full repository checked out at the correct commit.\n\n"
                f"Bug report:\n{instance.problem_statement}"
            )
            result: dict[str, Any] = dict(await pipeline.execute(request))
            elapsed = time.monotonic() - start

            # The run's changes: its commits on top of the base commit, else the
            # last patch's diff
            patch_set = result.get("current_patch") or {}
            generated_patch = _run_diff(repo_dir, instance.base_commit) or patch_set.get(
                "diff_unified", ""
            )

            # Compute quality score
            review = result.get("review_report") or {}
            test_report = result.get("test_report")
            emcs = compute_score(review, test_report) if review else 0.0

            # Evaluate: does the patch actually resolve the issue?
            resolved = _evaluate_patch(instance, generated_patch, repo_dir)

            return SWEBenchResult(
                instance_id=instance.instance_id,
                tier=instance.tier,
                resolved=resolved,
                status=result.get("status", "unknown"),
                iterations=result.get("iteration", 0),
                cost_usd=result.get("total_cost_usd", 0.0),
                elapsed_seconds=round(elapsed, 1),
                emcs_score=emcs,
                patch_generated=generated_patch,
            )

        except Exception as e:
            elapsed = time.monotonic() - start
            logger.exception("Instance %s failed: %s", instance.instance_id, e)
            # Capture partial work from pipeline state if available
            partial_patch = generated_patch
            partial_iterations = 0
            partial_cost = 0.0
            if state is not None:
                partial_patch = partial_patch or (
                    (state.get("current_patch") or {}).get("diff_unified", "")
                )
                partial_iterations = state.get("iteration", 0)
                partial_cost = state.get("total_cost_usd", 0.0)
            return SWEBenchResult(
                instance_id=instance.instance_id,
                tier=instance.tier,
                resolved=False,
                status="error",
                iterations=partial_iterations,
                cost_usd=partial_cost,
                elapsed_seconds=round(elapsed, 1),
                emcs_score=0.0,
                patch_generated=partial_patch,
                error=str(e),
            )


def compare_reports(*report_paths: Path) -> dict[str, Any]:
    """Compare multiple SWE-bench reports side-by-side.

    Returns a comparison table with per-tier breakdowns.
    """
    reports: list[dict[str, Any]] = []
    for path in report_paths:
        reports.append(json.loads(path.read_text()))

    comparison: dict[str, Any] = {
        "configs": [],
        "overall": [],
        "by_tier": {},
    }

    for report in reports:
        comparison["configs"].append(
            {
                "preset": report["preset"],
                "split": report["split"],
                "instances": report["completed"],
            }
        )
        comparison["overall"].append(
            {
                "preset": report["preset"],
                "resolve_rate": report["resolve_rate"],
                "total_cost": report["total_cost_usd"],
                "avg_iterations": report["avg_iterations"],
                "total_time": report["total_time_seconds"],
            }
        )

        for tier_name, tier_data in report.get("by_tier", {}).items():
            if tier_name not in comparison["by_tier"]:
                comparison["by_tier"][tier_name] = []
            comparison["by_tier"][tier_name].append(
                {
                    "preset": report["preset"],
                    **tier_data,
                }
            )

    return comparison
