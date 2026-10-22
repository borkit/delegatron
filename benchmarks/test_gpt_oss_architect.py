"""Supplementary test: GPT-OSS 20B as architect on 3 easiest SWE-bench bugs.

Isolated test — does not re-run qwen/phi4. Just confirms whether gpt-oss:20b
produces relevant plans for real bug reports.
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


async def test_one(model: str, inst) -> dict:
    provider = OpenAIProvider(
        api_key="ollama",
        model=model,
        base_url="http://localhost:11434/v1",
    )
    system = load_prompt("architect", 4)
    user = f"Bug report:\n{inst.problem_statement[:2000]}"

    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            provider.generate(system_prompt=system, user_message=user, temperature=0.2),
            timeout=420,
        )
        elapsed = time.monotonic() - start
        content = getattr(resp, "content", "") or ""
        # Try to extract summary
        try:
            parsed = json.loads(content)
            summary = parsed.get("summary", "(no summary field)")
        except Exception:
            summary = content[:300]

        # Keyword overlap check
        bug_kws = set()
        for word in inst.problem_statement[:300].lower().split():
            if len(word) > 4 and word.isalnum():
                bug_kws.add(word)
        sum_low = summary.lower()
        hits = sum(1 for kw in list(bug_kws)[:10] if kw in sum_low)
        on_topic = hits >= 2

        print(
            f"  [{inst.instance_id[:35]:35s}] {elapsed:5.1f}s "
            f"{'ON-TOPIC' if on_topic else 'OFF-TOPIC'} ({hits} kw hits)",
            flush=True,
        )
        print(f"    Plan: {summary[:140]}", flush=True)
        return {
            "instance_id": inst.instance_id,
            "elapsed_s": round(elapsed, 1),
            "on_topic": on_topic,
            "keyword_hits": hits,
            "summary": summary[:300],
        }
    except TimeoutError:
        elapsed = time.monotonic() - start
        print(f"  [{inst.instance_id[:35]}] TIMEOUT after {elapsed:.0f}s", flush=True)
        return {"instance_id": inst.instance_id, "elapsed_s": round(elapsed, 1), "error": "timeout"}
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"  [{inst.instance_id[:35]}] ERROR: {e}", flush=True)
        return {"instance_id": inst.instance_id, "elapsed_s": round(elapsed, 1), "error": str(e)[:150]}


async def main() -> None:
    model = "gpt-oss:20b"
    print(f"=== Testing {model} as architect on 3 SWE-bench instances ===\n", flush=True)

    all_simple = load_instances("lite", limit=None, tier="simple")
    sorted_inst = sorted(all_simple, key=lambda x: len(x.problem_statement))
    test_inst = sorted_inst[:3]
    print(f"Instances: {[i.instance_id for i in test_inst]}\n", flush=True)

    results = []
    for inst in test_inst:
        r = await test_one(model, inst)
        results.append(r)

    out = Path(__file__).resolve().parents[1] / "logs" / "gpt-oss-architect-results.json"
    out.write_text(json.dumps({"model": model, "results": results}, indent=2))
    print(f"\nResults saved to {out}", flush=True)

    on_topic = sum(1 for r in results if r.get("on_topic"))
    avg_time = sum(r.get("elapsed_s", 0) for r in results) / max(1, len(results))
    print(f"\nSummary: {on_topic}/{len(results)} on-topic, avg {avg_time:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
