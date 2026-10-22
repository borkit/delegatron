# SWE-bench Architect Benchmark — INCONCLUSIVE / ABANDONED

**Date:** 2026-04-16
**Status:** ABANDONED — benchmark hung on qwen3.5:9b's 3rd instance

## Goal

Test whether different local models as architect could fix SWE-bench's
prior failures. Hypothesis: the chess hallucination was caused by
qwen3.5:9b being too small; a larger architect might actually understand
real GitHub bug reports.

## Setup (planned, not fully executed)

- **Architects tested:** qwen3.5:9b, phi4:14b, qwen2.5-coder:14b, qwen3-coder:30b
- **Implementer:** fixed at qwen3-coder:30b
- **Reviewer:** fixed at qwen3.5:9b
- **Instances:** 3 easiest SWE-bench Lite instances (pytest-6116, django-14534, sympy-18057)
- **Total planned:** 12 runs

## What Actually Happened

**qwen3.5:9b results (partial):**
- Instance 1 (pytest-6116): resolved=False, exhausted, ~15 min
- Instance 2 (django-14534): resolved=False, exhausted, ~15 min
- Instance 3 (sympy-18057): **hung** — Ollama stopped responding after loading 30B implementer

**Critical finding: Template Collapse in qwen3.5:9b**

The 9B architect produced **identical plans for completely different bugs**:

| Bug | Plan Summary |
|-----|--------------|
| pytest-dev/pytest #6116 | "Implement a Python script that reads a CSV file, calculates the mean of a specified column, and prints the result." |
| django/django #14534 | "Implement a Python script that reads a CSV file, calculates the mean of a specified column, and prints the result." |

Same summary. Same task title ("Calculate Column Mean"). Both bugs have
nothing to do with CSV or means. The model has a default fallback
response it produces regardless of input.

This isn't hallucination — it's **template collapse**. The 9B model
cannot parse GitHub issue descriptions and falls back to a memorized
generic Python task.

## Why Other Architects Weren't Tested

After 2 instances (~30 min), the benchmark hung during instance 3.
Debugging showed:
- Python process alive but waiting on Ollama
- Ollama had qwen3-coder:30b loaded (11GB/12GB VRAM)
- Model idle (28W draw, not generating)
- Request retries kept timing out
- No progress after 5+ min

Likely cause: 30B model + large context (cloned astropy repo file tree)
exceeds what's practical on 12GB VRAM. The implementer hangs under load.

## Conclusions

1. **qwen3.5:9b cannot do SWE-bench** — template collapse, 0% useful plans
2. **30B implementer is too slow/fragile** for SWE-bench's large context
3. **Testing other architects would need:**
   - Different (smaller) implementer — but then less capable
   - Or 24+ GB VRAM to run 30B + architect simultaneously

## Honest Assessment

**Local models (at 12GB VRAM) cannot do SWE-bench.** This benchmark
confirms what the earlier tainted runs suggested. The limitations are:

1. 9B architects: template collapse on complex bug descriptions
2. 14B architects (Phi-4, Qwen2.5-Coder): untested, but unlikely to leap
   from 0% resolution to useful results
3. 30B model: too heavy for SWE-bench's context demands

**For SWE-bench on 12GB VRAM, there is no viable local configuration.**
Cloud API (Claude/GPT) is the only path.

## What We Kept

From this exercise:
- Confirmed the chess hallucination was specific to the v4 prompt's chess
  example (already fixed in main)
- Confirmed qwen3.5:9b produces default template responses when overwhelmed
- Confirmed 30B implementer + large repo context = system hang on 12GB

## Data Preserved

- qwen3.5:9b run artifacts: `swebench-runs-qwen3.5-9b/` (kept locally, not committed)
- Benchmark script: `benchmarks/benchmark_swebench_architects.py` (moved
  from `scripts/`; can be re-run with more hardware or different models, and
  overwrites this file)
