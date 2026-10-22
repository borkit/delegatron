"""Benchmark 4 local models as SWE-bench architect.

Fixed: implementer=qwen3-coder:30b, reviewer=qwen3.5:9b
Variable: architect (one of 4 models)
Test: 3 simple SWE-bench Lite instances per architect = 12 runs total.

Saves results to docs/swebench-architect-benchmark.md
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegatron.config import Config, PipelineConfig, RoleConfig
from delegatron.swebench import SWEBenchAdapter, load_instances

ARCHITECTS = [
    ("qwen3.5:9b", "Qwen 3.5 9B (current)"),
    ("phi4:14b", "Phi-4 14B"),
    ("qwen2.5-coder:14b", "Qwen2.5-Coder 14B"),
    ("qwen3-coder:30b", "Qwen3-Coder 30B"),
]

IMPLEMENTER = "qwen3-coder:30b"
REVIEWER = "qwen3.5:9b"
BASE_URL = "http://localhost:11434/v1"


@dataclass
class ArchitectResult:
    model: str
    label: str
    instances: list[dict] = field(default_factory=list)
    total_resolved: int = 0
    total_instances: int = 0
    avg_iterations: float = 0.0
    avg_time_s: float = 0.0
    error: str = ""


def make_config(architect_model: str) -> Config:
    """Build pipeline config with the specified architect."""
    return Config(
        architect=RoleConfig(
            provider="openai-compatible",
            model=architect_model,
            base_url=BASE_URL,
            prompt_version=4,
            max_tokens=16384,
        ),
        implementer=RoleConfig(
            provider="openai-compatible",
            model=IMPLEMENTER,
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
            max_iterations=3,
            max_cost_usd=10.0,
            max_wall_time_minutes=30,
            emcs_threshold=0.6,
        ),
    )


def write_progress(results: list[ArchitectResult], complete: bool = False) -> None:
    """Write current progress to report file (updates incrementally)."""
    report_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "swebench-architect-benchmark.md"
    )

    lines = [
        "# SWE-bench Architect Benchmark",
        "",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}",
        f"**Status:** {'COMPLETE' if complete else 'IN PROGRESS'}",
        "",
        "## Setup",
        "",
        f"- **Implementer:** `{IMPLEMENTER}` (fixed)",
        f"- **Reviewer:** `{REVIEWER}` (fixed)",
        "- **Test set:** 3 simple SWE-bench Lite instances",
        "- **Max iterations:** 3 per instance",
        "- **Architects tested:** 4 local models",
        "",
        "## Results Matrix",
        "",
        "| Architect | Resolved | Avg Iter | Avg Time | Plans were on-topic? |",
        "|-----------|----------|----------|----------|---------------------|",
    ]

    for r in results:
        on_topic_count = sum(1 for i in r.instances if i.get("plan_on_topic", False))
        on_topic = f"{on_topic_count}/{len(r.instances)}" if r.instances else "—"
        if r.error:
            lines.append(
                f"| {r.label} | ERROR | — | — | {r.error[:60]} |"
            )
        else:
            lines.append(
                f"| {r.label} | {r.total_resolved}/{r.total_instances} | "
                f"{r.avg_iterations:.1f} | {r.avg_time_s:.0f}s | {on_topic} |"
            )

    lines.extend(
        [
            "",
            "## Detailed Per-Instance Results",
            "",
        ]
    )

    for r in results:
        lines.append(f"### {r.label} (`{r.model}`)")
        lines.append("")
        if r.error:
            lines.append(f"⚠️ ERROR: {r.error}")
            lines.append("")
            continue
        if not r.instances:
            lines.append("*(running...)*")
            lines.append("")
            continue

        lines.append(
            "| Instance | Resolved | Status | Iter | Time | Plan Summary |"
        )
        lines.append(
            "|----------|----------|--------|------|------|--------------|"
        )
        for i in r.instances:
            res = "YES" if i.get("resolved") else "no"
            plan = i.get("plan_summary", "")[:80]
            lines.append(
                f"| {i.get('instance_id', '?')[:30]} | {res} | "
                f"{i.get('status', '?')} | {i.get('iterations', '?')} | "
                f"{i.get('elapsed_seconds', 0):.0f}s | {plan}... |"
            )
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


async def run_architect(
    architect_model: str,
    label: str,
    instance_ids: list[str],
) -> ArchitectResult:
    """Run all 3 test instances with the given architect."""
    print(f"\n{'='*60}", flush=True)
    print(f"ARCHITECT: {label} ({architect_model})", flush=True)
    print(f"{'='*60}", flush=True)

    result = ArchitectResult(model=architect_model, label=label)

    try:
        # Create custom adapter with our config
        work_dir = Path(f"swebench-workdir-{architect_model.replace(':', '-').replace('/', '-')}")
        work_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = Path(
            f"swebench-runs-{architect_model.replace(':', '-').replace('/', '-')}"
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # SWEBenchAdapter loads its config from a preset, so the run loop is
        # driven here directly with a custom config.
        from dataclasses import replace

        from delegatron.orchestrator.graph import Pipeline
        from delegatron.swebench import _evaluate_patch, _setup_repo, compute_score

        # Load instances
        all_instances = load_instances("lite", limit=None, tier="simple")
        instances = [i for i in all_instances if i.instance_id in instance_ids]
        if not instances:
            result.error = f"No matching instances found. Wanted: {instance_ids}"
            return result

        total_iters = 0
        total_time = 0.0

        for instance in instances:
            inst_start = time.monotonic()
            inst_data: dict = {
                "instance_id": instance.instance_id,
                "resolved": False,
                "status": "error",
                "iterations": 0,
                "elapsed_seconds": 0.0,
                "plan_summary": "",
                "plan_on_topic": False,
                "emcs_score": 0.0,
            }

            try:
                repo_dir = _setup_repo(instance, work_dir)
                config = make_config(architect_model)
                run_id = f"arch-{architect_model.split(':')[0].replace('.', '-')}-{instance.instance_id.replace('/', '-')}"[:60]

                pipeline = Pipeline(
                    config=config,
                    workspace_root=repo_dir,
                    run_id=run_id,
                    artifacts_dir=artifacts_dir,
                )

                # Simplified problem statement (our fix from earlier)
                request = (
                    f"Fix this bug in the {instance.repo} repository.\n\n"
                    f"IMPORTANT: This is a REAL bug fix in an existing codebase. "
                    f"Do NOT create a new project or new files from scratch. "
                    f"Find the bug in the EXISTING source files in the workspace "
                    f"and produce a minimal fix. The workspace already contains the "
                    f"full repository checked out at the correct commit.\n\n"
                    f"Bug report:\n{instance.problem_statement}"
                )

                state = dict(await pipeline.execute(request))
                elapsed = time.monotonic() - inst_start

                patch_set = state.get("current_patch") or {}
                generated_patch = patch_set.get("diff_unified", "")
                review = state.get("review_report") or {}
                test_report = state.get("test_report")
                emcs = compute_score(review, test_report) if review else 0.0
                resolved = _evaluate_patch(instance, generated_patch, repo_dir)

                # Read plan summary from work_plan.json
                work_plan_file = artifacts_dir / run_id / "work_plan.json"
                plan_summary = ""
                if work_plan_file.exists():
                    try:
                        plan_data = json.loads(work_plan_file.read_text())
                        plan_summary = plan_data.get("summary", "")
                    except Exception:
                        pass

                # Heuristic: is the plan about the actual bug?
                bug_keywords = (
                    instance.repo.split("/")[-1].lower().split("_")
                    + instance.problem_statement.lower().split()[:20]
                )
                on_topic = any(
                    kw in plan_summary.lower()
                    for kw in bug_keywords
                    if len(kw) > 4
                )

                inst_data.update(
                    {
                        "resolved": resolved,
                        "status": state.get("status", "unknown"),
                        "iterations": state.get("iteration", 0),
                        "elapsed_seconds": round(elapsed, 1),
                        "plan_summary": plan_summary,
                        "plan_on_topic": on_topic,
                        "emcs_score": emcs,
                    }
                )
                total_iters += inst_data["iterations"]
                total_time += elapsed
                if resolved:
                    result.total_resolved += 1

                print(
                    f"  [{instance.instance_id[:30]}] "
                    f"resolved={resolved} status={state.get('status')} "
                    f"iter={inst_data['iterations']} "
                    f"time={elapsed:.0f}s on_topic={on_topic}",
                    flush=True,
                )
                print(f"    plan: {plan_summary[:100]}", flush=True)

            except Exception as e:
                elapsed = time.monotonic() - inst_start
                inst_data["elapsed_seconds"] = round(elapsed, 1)
                inst_data["status"] = f"error: {str(e)[:100]}"
                total_time += elapsed
                print(f"  [{instance.instance_id}] ERROR: {e}", flush=True)
                traceback.print_exc()

            result.instances.append(inst_data)
            result.total_instances += 1

            # Progress is printed per instance; the report is written at the end.

        if result.total_instances > 0:
            result.avg_iterations = total_iters / result.total_instances
            result.avg_time_s = total_time / result.total_instances

    except Exception as e:
        result.error = str(e)[:200]
        print(f"FATAL ERROR: {e}", flush=True)
        traceback.print_exc()

    return result


async def main() -> None:
    # Pick 3 simple instances (smallest gold patches)
    all_simple = load_instances("lite", limit=None, tier="simple")
    # Sort by patch size, take smallest 3
    sorted_inst = sorted(all_simple, key=lambda i: len(i.patch))
    test_instances = [i.instance_id for i in sorted_inst[:3]]
    print(f"Test instances: {test_instances}", flush=True)
    print()

    results: list[ArchitectResult] = []

    # Initialize empty results for all architects
    for model, label in ARCHITECTS:
        results.append(ArchitectResult(model=model, label=label))

    for idx, (model, label) in enumerate(ARCHITECTS):
        result = await run_architect(model, label, test_instances)
        results[idx] = result
        # Write progress after each architect completes
        write_progress(results, complete=(idx == len(ARCHITECTS) - 1))

    # Final summary
    print("\n" + "=" * 60, flush=True)
    print("ALL ARCHITECT TESTS COMPLETE", flush=True)
    print("=" * 60, flush=True)
    for r in results:
        print(
            f"{r.label:30s} {r.total_resolved}/{r.total_instances} resolved, "
            f"avg {r.avg_time_s:.0f}s",
            flush=True,
        )

    write_progress(results, complete=True)


if __name__ == "__main__":
    asyncio.run(main())
