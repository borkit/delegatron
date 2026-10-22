"""Run benchmark: 4 models × 3 tasks = 12 local pipeline runs.

Usage: python benchmarks/run_model_benchmark.py
Results saved to docs/model-benchmark-results.md
"""

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# Must be at top before delegatron imports
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegatron.config import Config, PipelineConfig, RoleConfig, load_config
from delegatron.orchestrator.graph import Pipeline


# ── Models to test ──────────────────────────────────────────────────
MODELS = [
    ("qwen3-coder:30b", "Qwen3-Coder 30B"),
]

# ── Tasks ───────────────────────────────────────────────────────────
TASKS = {
    "fizzbuzz": {
        "name": "FizzBuzz",
        "difficulty": "Easy",
        "request": (
            "Create a FizzBuzz module. ALL files in a SINGLE task.\n\n"
            "Files needed:\n"
            "- fizzbuzz.py: function fizzbuzz(n) returns list of strings. "
            "For 1-n: 'Fizz' if div by 3, 'Buzz' if div by 5, 'FizzBuzz' if both, else str(number).\n"
            "- test_fizzbuzz.py: pytest tests covering normal numbers, Fizz, Buzz, FizzBuzz, edge cases (1, 15, 100).\n"
            "- requirements.txt: pytest only\n"
        ),
    },
    "fastapi": {
        "name": "FastAPI CRUD",
        "difficulty": "Medium",
        "request": (
            "Create a FastAPI app. ALL files in a SINGLE task — do not decompose into multiple tasks.\n\n"
            "Files needed:\n"
            "- main.py: FastAPI app with /health (returns {status: ok, uptime_seconds: float}) "
            "and /users CRUD (GET list, GET by id, POST create, PUT update, DELETE)\n"
            "- database.py: synchronous sqlite3 with init_db(), get_db() functions, "
            "users table (id INTEGER PRIMARY KEY, name TEXT, email TEXT)\n"
            "- test_main.py: pytest tests using TestClient for all endpoints "
            "(health returns 200, CRUD operations work, 404 on missing user, validation errors)\n"
            "- requirements.txt: fastapi, uvicorn, pytest, httpx\n\n"
            "Keep it simple. One file per concern. No Pydantic models file — define inline."
        ),
    },
    "chess": {
        "name": "Chess Engine",
        "difficulty": "Hard",
        "request": (
            "Create a chess game with GUI. ALL files in a SINGLE task — do not decompose.\n\n"
            "Files needed:\n"
            "- chess_engine.py: Board as 8x8 list of lists. None=empty, uppercase=white "
            "(R,N,B,Q,K,P), lowercase=black. Row 0=black back rank, row 7=white back rank. "
            "Functions: init_board(), is_valid_move(board,fr,fc,tr,tc)->bool (geometry+piece rules "
            "only, NOT turn enforcement), make_move(board,fr,fc,tr,tc)->bool (validates+enforces "
            "turn order), get_all_valid_moves(board,color)->list, is_in_check(board,color)->bool, "
            "is_checkmate(board,color)->bool\n"
            "- chess_gui.py: Tkinter 8x8 grid, click-to-select, click-to-move, highlight valid moves\n"
            "- test_chess_engine.py: pytest tests for init_board, each piece type, pawn diagonal "
            "captures, check, checkmate, turn enforcement\n"
            "- requirements.txt: pytest only (tkinter is stdlib)\n\n"
            "CRITICAL: is_valid_move checks geometry ONLY — not turn. Turn enforced in make_move. "
            "Tests import from chess_engine directly."
        ),
    },
}

ARCHITECT_MODEL = "qwen3.5:9b"
BASE_URL = "http://localhost:11434/v1"
# Scratch workspaces for the benchmark runs; override with DELEGATRON_BENCHMARK_WORKSPACE.
WORKSPACE_ROOT = Path(
    os.environ.get(
        "DELEGATRON_BENCHMARK_WORKSPACE",
        str(Path.home() / "delegatron-workspace" / "model-benchmark"),
    )
)
MAX_ITERATIONS = 3


@dataclass
class RunResult:
    model: str
    model_label: str
    task: str
    task_name: str
    difficulty: str
    status: str
    tests_passed: bool
    tests_total: int
    tests_failures: int
    emcs_score: float
    iterations: int
    elapsed_seconds: float
    error: str = ""


async def run_single(
    model: str, model_label: str, task_key: str, task: dict,
) -> RunResult:
    """Run a single model on a single task."""
    workspace = WORKSPACE_ROOT / f"{task_key}-{model.replace(':', '-').replace('/', '-')}"
    if workspace.exists():
        try:
            shutil.rmtree(workspace)
        except OSError:
            # Windows file locking — try harder
            import subprocess
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(workspace)],
                           capture_output=True, check=False)
    workspace.mkdir(parents=True, exist_ok=True)

    run_id = f"bench-{task_key}-{model.split(':')[0].replace('.', '-')}"

    config = Config(
        architect=RoleConfig(
            provider="openai-compatible",
            model=ARCHITECT_MODEL,
            base_url=BASE_URL,
            prompt_version=4,
        ),
        implementer=RoleConfig(
            provider="openai-compatible",
            model=model,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        reviewer=RoleConfig(
            provider="openai-compatible",
            model=ARCHITECT_MODEL,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        pipeline=PipelineConfig(
            max_iterations=MAX_ITERATIONS,
            max_cost_usd=10.0,
            max_wall_time_minutes=60,
            emcs_threshold=0.8,
        ),
    )

    start = time.monotonic()
    try:
        pipeline = Pipeline(config=config, workspace_root=workspace, run_id=run_id)
        state = await pipeline.execute(task["request"])
        elapsed = time.monotonic() - start

        # Extract results
        test_report = state.get("test_report") or {}
        review = state.get("review_report") or {}

        return RunResult(
            model=model,
            model_label=model_label,
            task=task_key,
            task_name=task["name"],
            difficulty=task["difficulty"],
            status=state.get("status", "unknown"),
            tests_passed=test_report.get("passed", False),
            tests_total=test_report.get("total", 0),
            tests_failures=test_report.get("failures", 0),
            emcs_score=state.get("emcs_score", 0.0),
            iterations=state.get("iteration", 0),
            elapsed_seconds=round(elapsed, 1),
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        return RunResult(
            model=model,
            model_label=model_label,
            task=task_key,
            task_name=task["name"],
            difficulty=task["difficulty"],
            status="error",
            tests_passed=False,
            tests_total=0,
            tests_failures=0,
            emcs_score=0.0,
            iterations=0,
            elapsed_seconds=round(elapsed, 1),
            error=str(e)[:200],
        )


def write_report(results: list[RunResult]) -> None:
    """Write markdown report."""
    report_path = Path(__file__).resolve().parents[1] / "docs" / "model-benchmark-results.md"

    lines = [
        "# Local Model Benchmark Results",
        "",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "",
        "4 models tested as implementer, qwen3.5:9b as architect/reviewer, v4 prompts.",
        f"Max iterations: {MAX_ITERATIONS}, single-task prompts, Docker sandbox.",
        "",
        "## Results Matrix",
        "",
        "| Model | FizzBuzz (Easy) | FastAPI (Medium) | Chess (Hard) |",
        "|-------|----------------|-----------------|-------------|",
    ]

    # Group by model
    by_model: dict[str, dict[str, RunResult]] = {}
    for r in results:
        by_model.setdefault(r.model_label, {})[r.task] = r

    for model_label, tasks in by_model.items():
        cells = []
        for task_key in ["fizzbuzz", "fastapi", "chess"]:
            r = tasks.get(task_key)
            if r is None:
                cells.append("—")
            elif r.status == "approved":
                cells.append(f"**APPROVED** {r.tests_total - r.tests_failures}/{r.tests_total} ({r.elapsed_seconds:.0f}s)")
            elif r.tests_passed:
                cells.append(f"Passed {r.tests_total - r.tests_failures}/{r.tests_total} EMCS {r.emcs_score:.2f} ({r.elapsed_seconds:.0f}s)")
            elif r.tests_total > 0:
                cells.append(f"{r.tests_total - r.tests_failures}/{r.tests_total} tests ({r.status}, {r.elapsed_seconds:.0f}s)")
            else:
                cells.append(f"{r.status} ({r.elapsed_seconds:.0f}s)")
        lines.append(f"| {model_label} | {' | '.join(cells)} |")

    lines.extend([
        "",
        "## Detailed Results",
        "",
    ])

    for r in results:
        status_icon = "✅" if r.status == "approved" else "⚠️" if r.tests_total > 0 else "❌"
        lines.append(
            f"- {status_icon} **{r.model_label}** on {r.task_name} ({r.difficulty}): "
            f"{r.status}, {r.tests_total - r.tests_failures}/{r.tests_total} tests, "
            f"EMCS {r.emcs_score:.2f}, {r.iterations} iters, {r.elapsed_seconds:.0f}s"
            + (f" — {r.error}" if r.error else "")
        )

    lines.extend([
        "",
        "## Comparison with Cloud Baseline (Claude Sonnet)",
        "",
        "| Task | Claude Sonnet | Best Local |",
        "|------|--------------|-----------|",
    ])

    cloud_baselines = {
        "fizzbuzz": "N/A (too simple)",
        "fastapi": "APPROVED 13/13 EMCS 1.0 ($0.10)",
        "chess": "40/41 EMCS 0.75 ($0.43)",
    }

    for task_key in ["fizzbuzz", "fastapi", "chess"]:
        best = None
        for r in results:
            if r.task == task_key:
                if best is None or (r.tests_total - r.tests_failures) > (best.tests_total - best.tests_failures):
                    best = r
        best_str = f"{best.model_label}: {best.tests_total - best.tests_failures}/{best.tests_total}" if best and best.tests_total > 0 else "None passed"
        lines.append(f"| {TASKS[task_key]['name']} | {cloud_baselines[task_key]} | {best_str} |")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to {report_path}")


async def main() -> None:
    results: list[RunResult] = []
    total = len(MODELS) * len(TASKS)
    current = 0

    for model, model_label in MODELS:
        for task_key, task in TASKS.items():
            current += 1
            print(f"\n{'='*60}")
            print(f"[{current}/{total}] {model_label} on {task['name']} ({task['difficulty']})")
            print(f"{'='*60}")

            result = await run_single(model, model_label, task_key, task)
            results.append(result)

            status_icon = "✅" if result.status == "approved" else "⚠️" if result.tests_total > 0 else "❌"
            print(f"\n{status_icon} Result: {result.status}, "
                  f"{result.tests_total - result.tests_failures}/{result.tests_total} tests, "
                  f"EMCS {result.emcs_score:.2f}, {result.elapsed_seconds:.0f}s")

            # Write intermediate report after each run
            write_report(results)

    print(f"\n{'='*60}")
    print("ALL BENCHMARKS COMPLETE")
    print(f"{'='*60}")
    write_report(results)


if __name__ == "__main__":
    asyncio.run(main())
