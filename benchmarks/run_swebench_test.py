"""Run SWE-bench evaluation — 1 instance each with Claude and local model.

Usage: python benchmarks/run_swebench_test.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegatron.swebench import SWEBenchAdapter


async def run_one(preset: str, label: str) -> dict:
    """Run 1 SWE-bench instance with a preset."""
    print(f"\n{'='*60}")
    print(f"SWE-bench: {label} (preset={preset})")
    print(f"{'='*60}")

    start = time.monotonic()
    try:
        adapter = SWEBenchAdapter(
            preset=preset,
            max_iterations=3,
            max_cost_usd=2.0,
        )
        report = await adapter.run(split="lite", limit=1)
        elapsed = time.monotonic() - start

        result = {
            "preset": preset,
            "label": label,
            "total": report.total_instances,
            "resolve_rate": report.resolve_rate,
            "cost": report.total_cost,
            "time": round(elapsed, 1),
            "results": [],
        }
        for r in report.results:
            result["results"].append({
                "instance": r.instance_id,
                "resolved": r.resolved,
                "status": r.status,
                "iterations": r.iterations,
                "cost": r.cost_usd,
                "emcs": r.emcs_score,
            })
            icon = "PASS" if r.resolved else "FAIL"
            print(f"  {icon} {r.instance_id}: status={r.status} iters={r.iterations} cost=${r.cost_usd:.4f}")

        print(f"\nResolve rate: {report.resolve_rate*100:.0f}% | Cost: ${report.total_cost:.4f} | Time: {elapsed:.0f}s")
        return result

    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"  ERROR: {e}")
        return {
            "preset": preset,
            "label": label,
            "error": str(e)[:200],
            "time": round(elapsed, 1),
        }


async def main():
    results = []

    # Test 1: Local model (free)
    results.append(await run_one("local", "Qwen3-Coder 30B (local)"))

    # Test 2: Claude (costs ~$0.50)
    results.append(await run_one("single-claude", "Claude Sonnet (cloud)"))

    # Save results
    report_path = Path(__file__).resolve().parents[1] / "docs" / "swebench-results.md"
    lines = [
        "# SWE-bench Evaluation Results",
        "",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "Split: lite, 1 instance each",
        "",
        "## Results",
        "",
        "| Preset | Instance | Resolved | Status | Iterations | Cost | Time |",
        "|--------|----------|----------|--------|-----------|------|------|",
    ]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['label']} | ERROR | — | {r['error'][:50]} | — | — | {r['time']}s |")
        else:
            for inst in r.get("results", []):
                resolved = "YES" if inst["resolved"] else "NO"
                lines.append(
                    f"| {r['label']} | {inst['instance']} | {resolved} | "
                    f"{inst['status']} | {inst['iterations']} | ${inst['cost']:.4f} | {r['time']}s |"
                )

    lines.extend(["", "## Raw Data", "", "```json", json.dumps(results, indent=2), "```"])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
