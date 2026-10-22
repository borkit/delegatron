"""Supplementary test for gpt-oss:20b.

Tests gpt-oss:20b against:
- 3 SWE-bench instances (as architect+implementer, reviewer=qwen3.5:9b)
- 2 standard tasks (FizzBuzz, FastAPI)

Reuses same instances/tasks as full_pipeline_validation.py for comparison.
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

MODEL = "gpt-oss:20b"
REVIEWER = "qwen3.5:9b"
BASE_URL = "http://localhost:11434/v1"
INSTANCE_TIMEOUT_S = 900
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "validation-results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = REPO_ROOT / "logs" / "gpt-oss-test.log"

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
            "- main.py: FastAPI app with /health and /users CRUD\n"
            "- database.py: synchronous sqlite3\n"
            "- test_main.py: pytest tests using TestClient\n"
            "- requirements.txt: fastapi, uvicorn, pytest, httpx\n"
        ),
    },
}


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def make_config(max_iters: int = 3) -> Config:
    return Config(
        architect=RoleConfig(
            provider="openai-compatible",
            model=MODEL,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        implementer=RoleConfig(
            provider="openai-compatible",
            model=MODEL,
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


async def unload_all() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        for m in [MODEL, REVIEWER]:
            try:
                await client.post(
                    "http://localhost:11434/api/generate",
                    json={"model": m, "keep_alive": 0},
                )
            except Exception:
                pass
    await asyncio.sleep(2)


async def run_swe(instance, work_dir, artifacts):
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
        config = make_config()
        run_id = f"gpt-oss-swe-{instance.instance_id.replace('/', '-')}"[:60]
        pipeline = Pipeline(
            config=config,
            workspace_root=repo_dir,
            run_id=run_id,
            artifacts_dir=artifacts,
        )
        request = (
            f"Fix this bug in the {instance.repo} repository.\n\n"
            f"IMPORTANT: This is a REAL bug fix. Do NOT create new files from scratch.\n\n"
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
        result.update({
            "resolved": resolved,
            "status": state.get("status", "unknown"),
            "iterations": state.get("iteration", 0),
            "elapsed_s": round(elapsed, 1),
            "emcs_score": emcs,
        })
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


async def run_std(task_key, task):
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
        workspace = OUTPUT_DIR / f"gpt-oss-std-{task_key}"
        if workspace.exists():
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass
        workspace.mkdir(parents=True, exist_ok=True)
        config = make_config()
        run_id = f"gpt-oss-std-{task_key}"[:60]
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
        tr = state.get("test_report") or {}
        result.update({
            "status": state.get("status", "unknown"),
            "tests_passed": tr.get("passed", False),
            "tests_total": tr.get("total", 0),
            "tests_failures": tr.get("failures", 0),
            "emcs_score": state.get("emcs_score", 0.0),
            "iterations": state.get("iteration", 0),
            "elapsed_s": round(elapsed, 1),
        })
    except asyncio.TimeoutError:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "timeout"
        result["error"] = f"exceeded {INSTANCE_TIMEOUT_S}s"
    except Exception as e:
        result["elapsed_s"] = round(time.monotonic() - start, 1)
        result["status"] = "error"
        result["error"] = str(e)[:300]
    return result


async def main():
    LOG_PATH.unlink(missing_ok=True)
    log(f"Starting gpt-oss:20b supplementary test")

    await unload_all()

    # SWE-bench
    log("\n=== SWE-bench (3 easiest) ===")
    all_simple = load_instances("lite", limit=None, tier="simple")
    test_instances = sorted(all_simple, key=lambda i: len(i.patch))[:3]
    work_dir = OUTPUT_DIR / "gpt-oss-swe-workdir"
    artifacts = OUTPUT_DIR / "gpt-oss-swe-runs"
    work_dir.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    swe_results = []
    for inst in test_instances:
        log(f"  [{inst.instance_id}] starting...")
        r = await run_swe(inst, work_dir, artifacts)
        swe_results.append(r)
        log(
            f"  [{inst.instance_id}] resolved={r['resolved']} "
            f"status={r['status']} iter={r['iterations']} time={r['elapsed_s']:.0f}s"
        )
        (OUTPUT_DIR / "gpt-oss-results.json").write_text(
            json.dumps({"swe": swe_results, "std": []}, indent=2)
        )

    # Standard tasks
    log("\n=== Standard tasks ===")
    std_results = []
    for task_key, task in STANDARD_TASKS.items():
        log(f"  [{task_key}] starting...")
        r = await run_std(task_key, task)
        std_results.append(r)
        log(
            f"  [{task_key}] status={r['status']} "
            f"tests={r['tests_total']-r['tests_failures']}/{r['tests_total']} "
            f"time={r['elapsed_s']:.0f}s"
        )
        (OUTPUT_DIR / "gpt-oss-results.json").write_text(
            json.dumps({"swe": swe_results, "std": std_results}, indent=2)
        )

    log("\n=== COMPLETE ===")
    log(f"SWE: {sum(1 for r in swe_results if r['resolved'])}/{len(swe_results)} resolved")
    for r in std_results:
        log(f"{r['task_name']}: {r['tests_total']-r['tests_failures']}/{r['tests_total']} ({r['status']})")


if __name__ == "__main__":
    asyncio.run(main())
