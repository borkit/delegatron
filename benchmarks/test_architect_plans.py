"""Lightweight architect test: just check if each local model produces a
relevant plan for SWE-bench bugs. No implementer, no sandbox.

Tests all 4 local models as architect against 3 easiest SWE-bench instances.
Checks: does the plan summary mention the bug's repo/topic, or is it a
default template response?
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegatron.agents.providers.openai_provider import OpenAIProvider
from delegatron.prompts import load_prompt
from delegatron.swebench import load_instances

ARCHITECTS = [
    ("qwen3.5:9b", "Qwen 3.5 9B"),
    ("phi4:14b", "Phi-4 14B"),
    ("qwen2.5-coder:14b", "Qwen2.5-Coder 14B"),
    ("qwen3-coder:30b", "Qwen3-Coder 30B"),
]


async def test_architect(model: str, label: str, instances: list) -> dict:
    """Test an architect against all instances. Returns results."""
    print(f"\n{'='*60}", flush=True)
    print(f"TESTING ARCHITECT: {label} ({model})", flush=True)
    print(f"{'='*60}", flush=True)

    provider = OpenAIProvider(
        api_key="ollama",
        model=model,
        base_url="http://localhost:11434/v1",
    )
    system_prompt = load_prompt("architect", 4)

    results = {"model": model, "label": label, "instances": []}

    for inst in instances:
        request = (
            f"Fix this bug in the {inst.repo} repository.\n\n"
            f"IMPORTANT: This is a REAL bug fix in an existing codebase. "
            f"Do NOT create a new project from scratch.\n\n"
            f"Bug report:\n{inst.problem_statement[:2000]}"
        )

        start = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                provider.generate(
                    system_prompt=system_prompt,
                    user_message=request,
                    max_tokens=4096,
                ),
                timeout=120.0,
            )
            elapsed = time.monotonic() - start
            content = resp.content

            # Extract summary if JSON
            summary = "(no JSON)"
            try:
                # Strip markdown fences and think tags
                import re

                clean = re.sub(
                    r"<think>.*?</think>", "", content, flags=re.DOTALL
                ).strip()
                if clean.startswith("```"):
                    lines = clean.split("\n")
                    lines = [ln for ln in lines[1:] if not ln.startswith("```")]
                    clean = "\n".join(lines).strip()
                if not clean.startswith("{"):
                    m = re.search(r"\{.*\}", clean, flags=re.DOTALL)
                    if m:
                        clean = m.group(0)
                data = json.loads(clean)
                summary = data.get("summary", "(no summary field)")
            except Exception as e:
                summary = f"(parse failed: {str(e)[:80]})"

            # Check if plan is on-topic for this bug
            repo_name = inst.repo.split("/")[-1].lower()
            bug_keywords = []
            # Pick meaningful words from problem statement
            for word in inst.problem_statement[:300].lower().split():
                w = word.strip(",.()[]{}:`\"'")
                if len(w) > 4 and w not in (
                    "the", "and", "that", "with", "from", "this", "have",
                    "which", "when", "what", "where", "because", "should",
                ):
                    bug_keywords.append(w)
            # Check summary for repo name or bug keywords
            summary_lower = summary.lower()
            on_topic = repo_name in summary_lower or any(
                kw in summary_lower for kw in bug_keywords[:10]
            )

            # Suspicious default templates
            templates = ["csv", "mean", "fizzbuzz", "calculator", "todo"]
            is_template = any(t in summary_lower for t in templates)

            print(
                f"  [{inst.instance_id[:35]:35s}] {elapsed:5.1f}s "
                f"on_topic={on_topic} template={is_template}",
                flush=True,
            )
            print(f"    Plan: {summary[:120]}", flush=True)

            results["instances"].append(
                {
                    "instance_id": inst.instance_id,
                    "repo": inst.repo,
                    "elapsed_s": round(elapsed, 1),
                    "summary": summary[:300],
                    "on_topic": on_topic,
                    "is_template": is_template,
                    "output_tokens": resp.output_tokens,
                }
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            print(f"  [{inst.instance_id[:35]}] TIMEOUT after {elapsed:.0f}s", flush=True)
            results["instances"].append(
                {
                    "instance_id": inst.instance_id,
                    "error": "timeout",
                    "elapsed_s": round(elapsed, 1),
                }
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            print(f"  [{inst.instance_id[:35]}] ERROR: {e}", flush=True)
            results["instances"].append(
                {
                    "instance_id": inst.instance_id,
                    "error": str(e)[:150],
                    "elapsed_s": round(elapsed, 1),
                }
            )

    return results


def write_report(all_results: list) -> None:
    report_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "architect-plan-test.md"
    )
    lines = [
        "# Architect Plan Quality Test",
        "",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "Tests each local model as architect against 3 SWE-bench bugs.",
        "Pure planning test — no implementer, no sandbox, no Docker.",
        "**Just asks: given a real GitHub bug report, does the model produce a relevant plan?**",
        "",
        "## Results Matrix",
        "",
        "| Architect | On-topic | Template Collapse | Avg Time | Avg Tokens |",
        "|-----------|----------|-------------------|----------|-----------|",
    ]

    for r in all_results:
        inst = r.get("instances", [])
        on_topic = sum(1 for i in inst if i.get("on_topic"))
        template = sum(1 for i in inst if i.get("is_template"))
        times = [i.get("elapsed_s", 0) for i in inst if "elapsed_s" in i]
        avg_time = sum(times) / len(times) if times else 0
        tokens = [i.get("output_tokens", 0) for i in inst if "output_tokens" in i]
        avg_tok = sum(tokens) / len(tokens) if tokens else 0
        total = len(inst)
        lines.append(
            f"| {r['label']} | {on_topic}/{total} | {template}/{total} | "
            f"{avg_time:.1f}s | {avg_tok:.0f} |"
        )

    lines.extend(["", "## Detailed Plans", ""])
    for r in all_results:
        lines.append(f"### {r['label']} (`{r['model']}`)")
        lines.append("")
        for i in r.get("instances", []):
            iid = i.get("instance_id", "?")
            if "error" in i:
                lines.append(f"- **{iid}**: ERROR — {i['error']}")
            else:
                on_topic = "✓" if i.get("on_topic") else "✗"
                template = "⚠️ TEMPLATE" if i.get("is_template") else ""
                lines.append(
                    f"- **{iid}** ({i.get('elapsed_s', 0):.1f}s, "
                    f"{i.get('output_tokens', 0)} tokens) {on_topic} {template}"
                )
                lines.append(f"  - Plan: {i.get('summary', '')}")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to {report_path}", flush=True)


async def main() -> None:
    # Get 3 simplest instances
    all_simple = load_instances("lite", limit=None, tier="simple")
    sorted_inst = sorted(all_simple, key=lambda i: len(i.patch))
    test_instances = sorted_inst[:3]
    print(f"Test instances: {[i.instance_id for i in test_instances]}", flush=True)

    all_results = []
    for model, label in ARCHITECTS:
        r = await test_architect(model, label, test_instances)
        all_results.append(r)
        # Write after each architect
        write_report(all_results)

    print("\n" + "=" * 60, flush=True)
    print("ALL ARCHITECTS TESTED", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
