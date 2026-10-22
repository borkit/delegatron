"""Full pipeline validation: test all 4 local models end-to-end.

Phase 1: SWE-bench (3 simple instances) with qwen3-coder:30b architect × 4 implementers
Phase 2: Standard tasks (FizzBuzz, FastAPI, Chess) — same model for all roles

Safeguards:
- Per-instance timeout (15 min)
- Ollama unload between model switches
- Fail-fast: skip architect if 2 consecutive hangs
- Incremental save after each run
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.orchestrator.graph import Pipeline
from delegatron.swebench import (
    _evaluate_patch,
    _setup_repo,
    compute_score,
    load_instances,
)

MODELS = [
    ("qwen3.5:9b", "Qwen 3.5 9B"),
    ("phi4:14b", "Phi-4 14B"),
    ("qwen2.5-coder:14b", "Qwen2.5-Coder 14B"),
    ("qwen3-coder:30b", "Qwen3-Coder 30B"),
]

ARCHITECT = "qwen3-coder:30b"  # Best from plan test
REVIEWER = "qwen3.5:9b"
BASE_URL = "http://localhost:11434/v1"

INSTANCE_TIMEOUT_S = 900  # 15 min hard cap
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "validation-results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = REPO_ROOT / "logs" / "validation.log"


def log(msg: str) -> None:
    """Log to both stdout and file."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def save_results(phase: str, results: list) -> None:
    path = OUTPUT_DIR / f"{phase}-results.json"
    path.write_text(json.dumps(results, indent=2, default=str))


async def unload_all_ollama() -> None:
    """Force-unload all models to free VRAM and reset state."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        for model, _ in MODELS:
            try:
                await client.post(
                    "http://localhost:11434/api/generate",
                    json={"model": model, "keep_alive": 0},
                )
            except Exception:
                pass
    await asyncio.sleep(2)


def make_config(architect: str, implementer: str, max_iters: int = 3) -> Config:
    return Config(
        architect=RoleConfig(
            provider="openai-compatible",
            model=architect,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        implementer=RoleConfig(
            provider="openai-compatible",
            model=implementer,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        reviewer=RoleConfig(
            provider="openai-compatible",
            model=REVIEWER,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        pipeline=PipelineConfig(
            max_iterations=max_iters,
            max_cost_usd=10.0,
            max_wall_time_minutes=30,
            emcs_threshold=0.6,
        ),
    )


# ═══════════════════════════════════════════════════════════════════
# PHASE 1: SWE-bench
# ═══════════════════════════════════════════════════════════════════


async def run_swebench_instance(
    config: Config, instance, work_dir: Path, artifacts_dir: Path
) -> dict:
    """Run one SWE-bench instance with the given config."""
    start = time.monotonic()
    result = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "resolved": False,
        "status": "unknown",
        "iterations": 0,
        "elapsed_s": 0.0,
        "emcs_score": 0.0,
        "error": "",
    }

    try:
        repo_dir = _setup_repo(instance, work_dir)
        run_id = (
            f"val-swe-{config.implementer.model.split(':')[0].replace('.', '-')}"
            f"-{instance.instance_id.replace('/', '-')}"
        )[:60]

        pipeline = Pipeline(
            config=config,
            workspace_root=repo_dir,
            run_id=run_id,
            artifacts_dir=artifacts_dir,
        )

        request = (
            f"Fix this bug in the {instance.repo} repository.\n\n"
            f"IMPORTANT: This is a REAL bug fix in an existing codebase. "
            f"Do NOT create a new project or new files from scratch. "
            f"Find the bug in the EXISTING source files in the workspace "
            f"and produce a minimal fix.\n\n"
            f"Bug report:\n{instance.problem_statement[:2000]}"
        )

        state = dict(
            await asyncio.wait_for(
                pipeline.execute(request), timeout=INSTANCE_TIMEOUT_S
            )
        )
        elapsed = time.monotonic() - start

        patch_set = state.get("current_patch") or {}
        generated_patch = patch_set.get("diff_unified", "")
        review = state.get("review_report") or {}
        test_report = state.get("test_report")
        emcs = compute_score(review, test_report) if review else 0.0
        resolved = _evaluate_patch(instance, generated_patch, repo_dir)

        result.update(
            {
                "resolved": resolved,
                "status": state.get("status", "unknown"),
                "iterations": state.get("iteration", 0),
                "elapsed_s": round(elapsed, 1),
                "emcs_score": emcs,
            }
        )
    except asyncio.TimeoutError:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "timeout"
        result["error"] = f"exceeded {INSTANCE_TIMEOUT_S}s"
    except Exception as e:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "error"
        result["error"] = str(e)[:300]
        traceback.print_exc()

    return result


async def phase1_swebench(test_instances: list) -> list:
    """Phase 1: SWE-bench with each model as implementer."""
    log("=" * 60)
    log("PHASE 1: SWE-bench validation")
    log(f"Architect: {ARCHITECT} (fixed)")
    log(f"Testing {len(MODELS)} implementers × {len(test_instances)} instances")
    log("=" * 60)

    all_results = []

    for model, label in MODELS:
        log(f"\n>>> IMPLEMENTER: {label} ({model})")
        await unload_all_ollama()

        work_dir = OUTPUT_DIR / f"swe-workdir-{model.replace(':', '-').replace('/', '-')}"
        artifacts = OUTPUT_DIR / f"swe-runs-{model.replace(':', '-').replace('/', '-')}"
        work_dir.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)

        config = make_config(ARCHITECT, model)
        implementer_results = {
            "model": model,
            "label": label,
            "instances": [],
        }

        consecutive_hangs = 0
        for inst in test_instances:
            log(f"  [{inst.instance_id}] starting...")
            r = await run_swebench_instance(config, inst, work_dir, artifacts)
            implementer_results["instances"].append(r)
            log(
                f"  [{inst.instance_id}] resolved={r['resolved']} "
                f"status={r['status']} iter={r['iterations']} "
                f"time={r['elapsed_s']:.0f}s"
            )

            if r["status"] in ("timeout", "error"):
                consecutive_hangs += 1
                if consecutive_hangs >= 2:
                    log(f"  !!! 2 consecutive hangs, skipping remaining instances")
                    break
            else:
                consecutive_hangs = 0

            # Save progress after each instance
            all_results_so_far = all_results + [implementer_results]
            save_results("phase1-swebench", all_results_so_far)

        all_results.append(implementer_results)
        save_results("phase1-swebench", all_results)

    return all_results


# ═══════════════════════════════════════════════════════════════════
# PHASE 2: Standard tasks
# ═══════════════════════════════════════════════════════════════════

STANDARD_TASKS = {
    "fizzbuzz": {
        "name": "FizzBuzz",
        "request": (
            "Create a FizzBuzz module. ALL files in a SINGLE task.\n\n"
            "- fizzbuzz.py: function fizzbuzz(n) returns list of strings for 1..n.\n"
            "  Div by 3 → 'Fizz'; div by 5 → 'Buzz'; both → 'FizzBuzz'; else str(n).\n"
            "- test_fizzbuzz.py: pytest tests covering edge cases (1, 15, 100).\n"
            "- requirements.txt: pytest only\n"
        ),
    },
    "fastapi": {
        "name": "FastAPI CRUD",
        "request": (
            "Create a FastAPI app. ALL files in a SINGLE task.\n\n"
            "- main.py: FastAPI app with /health (uptime) and /users CRUD "
            "(GET list, GET by id, POST create, PUT update, DELETE)\n"
            "- database.py: synchronous sqlite3, init_db() and get_db()\n"
            "- test_main.py: pytest tests using TestClient\n"
            "- requirements.txt: fastapi, uvicorn, pytest, httpx\n"
        ),
    },
}


async def run_standard_task(
    config: Config, task_key: str, task: dict
) -> dict:
    """Run one standard task."""
    start = time.monotonic()
    result = {
        "task_key": task_key,
        "task_name": task["name"],
        "status": "unknown",
        "tests_passed": False,
        "tests_total": 0,
        "tests_failures": 0,
        "emcs_score": 0.0,
        "iterations": 0,
        "elapsed_s": 0.0,
        "error": "",
    }

    try:
        impl_safe = config.implementer.model.replace(":", "-").replace("/", "-")
        workspace = OUTPUT_DIR / f"std-{task_key}-{impl_safe}"
        if workspace.exists():
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass
        workspace.mkdir(parents=True, exist_ok=True)

        run_id = f"val-std-{task_key}-{impl_safe}"[:60]
        pipeline = Pipeline(
            config=config,
            workspace_root=workspace,
            run_id=run_id,
        )

        state = dict(
            await asyncio.wait_for(
                pipeline.execute(task["request"]), timeout=INSTANCE_TIMEOUT_S
            )
        )
        elapsed = time.monotonic() - start
        test_report = state.get("test_report") or {}
        result.update(
            {
                "status": state.get("status", "unknown"),
                "tests_passed": test_report.get("passed", False),
                "tests_total": test_report.get("total", 0),
                "tests_failures": test_report.get("failures", 0),
                "emcs_score": state.get("emcs_score", 0.0),
                "iterations": state.get("iteration", 0),
                "elapsed_s": round(elapsed, 1),
            }
        )
    except asyncio.TimeoutError:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "timeout"
        result["error"] = f"exceeded {INSTANCE_TIMEOUT_S}s"
    except Exception as e:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "error"
        result["error"] = str(e)[:300]

    return result


async def phase2_standard() -> list:
    """Phase 2: Standard tasks with each model as implementer."""
    log("\n" + "=" * 60)
    log("PHASE 2: Standard task validation")
    log(f"Architect: {ARCHITECT} (fixed)")
    log(f"Testing {len(MODELS)} implementers × {len(STANDARD_TASKS)} tasks")
    log("=" * 60)

    all_results = []
    for model, label in MODELS:
        log(f"\n>>> IMPLEMENTER: {label} ({model})")
        await unload_all_ollama()

        config = make_config(ARCHITECT, model)
        model_results = {
            "model": model,
            "label": label,
            "tasks": [],
        }

        consecutive_fails = 0
        for task_key, task in STANDARD_TASKS.items():
            log(f"  [{task_key}] starting...")
            r = await run_standard_task(config, task_key, task)
            model_results["tasks"].append(r)
            log(
                f"  [{task_key}] status={r['status']} "
                f"tests={r['tests_total']-r['tests_failures']}/{r['tests_total']} "
                f"emcs={r['emcs_score']:.2f} time={r['elapsed_s']:.0f}s"
            )

            if r["status"] in ("timeout", "error"):
                consecutive_fails += 1
                if consecutive_fails >= 2:
                    log("  !!! 2 consecutive failures, skipping")
                    break
            else:
                consecutive_fails = 0

            save_results("phase2-standard", all_results + [model_results])

        all_results.append(model_results)
        save_results("phase2-standard", all_results)

    return all_results


# ═══════════════════════════════════════════════════════════════════
# PHASE 3: Report
# ═══════════════════════════════════════════════════════════════════


def phase3_report(swe_results: list, std_results: list) -> None:
    log("\n" + "=" * 60)
    log("PHASE 3: Generating comparison report")
    log("=" * 60)

    report_path = REPO_ROOT / "docs" / "full-pipeline-validation.md"
    lines = [
        "# Full Pipeline Validation Results",
        "",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}",
        f"**Architect:** `{ARCHITECT}` (fixed for all runs)",
        f"**Reviewer:** `{REVIEWER}` (fixed for all runs)",
        f"**Test timeout:** {INSTANCE_TIMEOUT_S}s per instance",
        "",
        "## Phase 1: SWE-bench (3 simple instances)",
        "",
        "| Implementer | Resolved | Errors | Avg Iter | Avg Time |",
        "|-------------|----------|--------|----------|----------|",
    ]
    for r in swe_results:
        inst = r.get("instances", [])
        resolved = sum(1 for i in inst if i.get("resolved"))
        errors = sum(1 for i in inst if i.get("status") in ("timeout", "error"))
        iters = [i.get("iterations", 0) for i in inst]
        times = [i.get("elapsed_s", 0) for i in inst]
        avg_iter = sum(iters) / len(iters) if iters else 0
        avg_time = sum(times) / len(times) if times else 0
        lines.append(
            f"| {r['label']} | {resolved}/{len(inst)} | {errors}/{len(inst)} | "
            f"{avg_iter:.1f} | {avg_time:.0f}s |"
        )

    lines.extend(
        [
            "",
            "## Phase 2: Standard tasks",
            "",
            "| Implementer | FizzBuzz | FastAPI CRUD |",
            "|-------------|----------|--------------|",
        ]
    )
    for r in std_results:
        tasks = {t["task_key"]: t for t in r.get("tasks", [])}
        cells = []
        for tk in ["fizzbuzz", "fastapi"]:
            t = tasks.get(tk)
            if t:
                if t["status"] == "approved":
                    cells.append(
                        f"**APPROVED** {t['tests_total']-t['tests_failures']}/"
                        f"{t['tests_total']}"
                    )
                elif t["tests_total"] > 0:
                    cells.append(
                        f"{t['tests_total']-t['tests_failures']}/{t['tests_total']} "
                        f"({t['status']})"
                    )
                else:
                    cells.append(f"{t['status']} ({t['elapsed_s']:.0f}s)")
            else:
                cells.append("—")
        lines.append(f"| {r['label']} | {cells[0]} | {cells[1]} |")

    lines.extend(["", "## Detailed Results", ""])
    lines.append("### Phase 1: SWE-bench")
    for r in swe_results:
        lines.append(f"\n**{r['label']}** (`{r['model']}`)")
        for i in r.get("instances", []):
            lines.append(
                f"- `{i['instance_id']}`: resolved={i.get('resolved')} "
                f"status={i.get('status')} iter={i.get('iterations', 0)} "
                f"time={i.get('elapsed_s', 0):.0f}s emcs={i.get('emcs_score', 0):.2f}"
                + (f" error={i['error'][:80]}" if i.get("error") else "")
            )

    lines.append("\n### Phase 2: Standard Tasks")
    for r in std_results:
        lines.append(f"\n**{r['label']}** (`{r['model']}`)")
        for t in r.get("tasks", []):
            lines.append(
                f"- {t['task_name']}: status={t['status']} "
                f"tests={t['tests_total']-t['tests_failures']}/{t['tests_total']} "
                f"emcs={t['emcs_score']:.2f} time={t['elapsed_s']:.0f}s"
                + (f" error={t['error'][:80]}" if t.get("error") else "")
            )

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Report saved to {report_path}")


async def main() -> None:
    LOG_PATH.unlink(missing_ok=True)
    log("Starting full pipeline validation")
    log(f"Output: {OUTPUT_DIR}")

    # Load test instances
    all_simple = load_instances("lite", limit=None, tier="simple")
    test_instances = sorted(all_simple, key=lambda i: len(i.patch))[:3]
    log(f"Test instances: {[i.instance_id for i in test_instances]}")

    # Phase 1: SWE-bench
    swe_results = await phase1_swebench(test_instances)

    # Phase 2: Standard tasks
    std_results = await phase2_standard()

    # Phase 3: Report
    phase3_report(swe_results, std_results)

    log("\n" + "=" * 60)
    log("FULL PIPELINE VALIDATION COMPLETE")
    log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
