#!/usr/bin/env python3
"""Lightweight benchmark for local Ollama model combinations.

Runs a simple coding task through the pipeline with different model
configurations and reports: pass/fail, iterations used, wall time,
and quality score.

Usage:
    python benchmarks/benchmark-local-models.py
    python benchmarks/benchmark-local-models.py --task fizzbuzz
    python benchmarks/benchmark-local-models.py --task calculator --max-iters 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASKS = {
    "fizzbuzz": (
        "Create a Python module fizzbuzz.py with a function fizzbuzz(n: int) -> str "
        "that returns 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, "
        "'FizzBuzz' for multiples of both, and the number as string otherwise. "
        "Also create test_fizzbuzz.py with pytest tests covering: normal numbers, "
        "multiples of 3, multiples of 5, multiples of 15, edge cases (0, 1, negative)."
    ),
    "calculator": (
        "Create calculator.py with a Calculator class that has methods: "
        "add(a, b), subtract(a, b), multiply(a, b), divide(a, b). "
        "divide should raise ValueError on division by zero. "
        "Create test_calculator.py with pytest tests for all operations "
        "including edge cases (zero, negative, float)."
    ),
    "todo_api": (
        "Create a Python module todo.py with a TodoList class. Methods: "
        "add(title: str) -> int (returns id), remove(id: int) -> bool, "
        "get(id: int) -> dict | None, list_all() -> list[dict], "
        "mark_done(id: int) -> bool. Each todo has id, title, done fields. "
        "Create test_todo.py with pytest tests covering add, remove, get, "
        "list_all, mark_done, and error cases (invalid id, duplicate remove)."
    ),
}

# Model combinations to benchmark.
# Each entry: (name, architect_model, implementer_model, reviewer_model)
COMBOS: list[tuple[str, str, str, str]] = [
    # Current best — baseline
    ("mix-v3", "qwen3.5:9b", "qwen3-coder:30b", "deepseek-r1:14b-qwen-distill-q4_K_M"),
    ("coder-only", "qwen3-coder:30b", "qwen3-coder:30b", "qwen3-coder:30b"),
    # New qwen3.5 — replace arch+rev with 3.5:9b (smaller, newer)
    ("q35-mix", "qwen3.5:9b", "qwen3-coder:30b", "qwen3.5:9b"),
    # New qwen3.5:27b — can it replace qwen3-coder:30b as implementer?
    ("q35-27b", "qwen3.5:27b", "qwen3.5:27b", "qwen3.5:27b"),
    # Hybrid: 3.5 arch, coder impl, 3.5 rev
    ("q35-arch", "qwen3.5:9b", "qwen3-coder:30b", "deepseek-r1:14b-qwen-distill-q4_K_M"),
]

OLLAMA_URL = "http://localhost:11434/v1"


@dataclass
class BenchResult:
    combo_name: str
    task: str
    status: str
    iterations: int
    approved: bool
    tests_passed: bool
    wall_seconds: float
    cost_usd: float
    error: str = ""


def make_config(arch_model: str, impl_model: str, rev_model: str, max_iters: int) -> Any:
    """Build a Config with the given model combination."""
    from delegatron.config import load_config
    from dataclasses import replace

    config = load_config(preset="local")
    # Override models
    config = replace(
        config,
        architect=replace(config.architect, model=arch_model),
        implementer=replace(config.implementer, model=impl_model),
        reviewer=replace(config.reviewer, model=rev_model),
        pipeline=replace(config.pipeline, max_iterations=max_iters, max_wall_time_minutes=30),
    )
    return config


async def run_benchmark(
    task_name: str,
    task_text: str,
    combo_name: str,
    arch: str,
    impl: str,
    rev: str,
    max_iters: int,
    workspace: Path,
) -> BenchResult:
    """Run a single benchmark configuration."""
    import uuid
    from delegatron.orchestrator.graph import Pipeline

    config = make_config(arch, impl, rev, max_iters)
    run_id = f"bench-{combo_name}-{task_name}-{uuid.uuid4().hex[:6]}"
    run_workspace = workspace / run_id
    run_workspace.mkdir(parents=True, exist_ok=True)

    print(f"  [{combo_name}] Starting: arch={arch}, impl={impl}, rev={rev}")

    pipeline = Pipeline(
        config=config,
        workspace_root=run_workspace,
        run_id=run_id,
    )

    start = time.monotonic()
    try:
        state = await pipeline.execute(task_text)
        elapsed = time.monotonic() - start

        return BenchResult(
            combo_name=combo_name,
            task=task_name,
            status=state.get("status", "unknown"),
            iterations=state.get("iteration", 0),
            approved=state.get("is_approved", False),
            tests_passed=(state.get("test_report") or {}).get("passed", False),
            wall_seconds=elapsed,
            cost_usd=state.get("total_cost_usd", 0),
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        return BenchResult(
            combo_name=combo_name,
            task=task_name,
            status="error",
            iterations=0,
            approved=False,
            tests_passed=False,
            wall_seconds=elapsed,
            cost_usd=0,
            error=str(e)[:200],
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local model combos")
    parser.add_argument("--task", default="fizzbuzz", choices=list(TASKS.keys()))
    parser.add_argument("--max-iters", type=int, default=5)
    parser.add_argument("--workspace", default="benchmark-workspace")
    parser.add_argument("--combos", nargs="*", help="Run only these combo names")
    args = parser.parse_args()

    task_text = TASKS[args.task]
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    combos = COMBOS
    if args.combos:
        combos = [c for c in combos if c[0] in args.combos]

    print(f"Benchmark: task={args.task}, max_iters={args.max_iters}")
    print(f"Testing {len(combos)} model combinations\n")

    results: list[BenchResult] = []
    for name, arch, impl, rev in combos:
        result = await run_benchmark(
            args.task, task_text, name, arch, impl, rev, args.max_iters, workspace,
        )
        results.append(result)
        symbol = "PASS" if result.approved else ("TESTS" if result.tests_passed else "FAIL")
        print(f"  [{name}] {symbol} — {result.iterations} iters, "
              f"{result.wall_seconds:.0f}s, ${result.cost_usd:.4f}")
        if result.error:
            print(f"    Error: {result.error}")
        print()

    # Summary table
    print("=" * 80)
    print(f"{'Combo':<15} {'Status':<12} {'Pass':<6} {'Tests':<6} "
          f"{'Iters':<6} {'Time':<8} {'Cost':<8}")
    print("-" * 80)
    for r in results:
        print(f"{r.combo_name:<15} {r.status:<12} "
              f"{'YES' if r.approved else 'no':<6} "
              f"{'YES' if r.tests_passed else 'no':<6} "
              f"{r.iterations:<6} {r.wall_seconds:>6.0f}s "
              f"${r.cost_usd:>6.4f}")

    # Save results
    out = workspace / f"bench-{args.task}-results.json"
    out.write_text(json.dumps([
        {
            "combo": r.combo_name, "task": r.task, "status": r.status,
            "approved": r.approved, "tests_passed": r.tests_passed,
            "iterations": r.iterations, "wall_seconds": r.wall_seconds,
            "cost_usd": r.cost_usd, "error": r.error,
        }
        for r in results
    ], indent=2))
    print(f"\nResults saved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
