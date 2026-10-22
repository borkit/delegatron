# Delegatron Benchmarks

Research/evaluation harness scripts — **not** part of the user-facing surface or the deployment path. These drive the pipeline on fixed tasks and datasets to produce the numbers in `docs/capabilities.md`, `docs/architect-plan-test.md`, `docs/model-benchmark-results.md`, and the SWE-bench write-ups.

Deployment + bootstrap scripts live in `scripts/`. Nothing in this folder is referenced by the CLI, server, or installers.

## Prerequisites

- A working Delegatron install (`pip install -e ".[dev]"`)
- Ollama running locally with the relevant models pulled (Phi-4:14b, Qwen2.5-Coder:14b, Qwen3-Coder:30b, etc.)
- For SWE-bench scripts: `pip install 'delegatron[swebench]'`
- For cloud comparisons: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`

The full-pipeline scripts run Delegatron end-to-end through the sandbox (Docker unless `DELEGATRON_SANDBOX` says otherwise); the plan-only scripts (`test_architect_plans.py`, `test_gpt_oss_architect.py`) call the architect model directly. Expect multi-hour walltimes on the full sweeps.

Most scripts build their roles by hand and pin `prompt_version = 4`, the prompts the published numbers were measured with. `benchmark-local-models.py` (which swaps in its own models) and `run_swebench_test.py` start from presets, so they now get the v5 prompts every local preset pins; `run_swebench_test.py` also gets the preset's current models (`local` is phi4 + qwen2.5-coder:14b + phi4, not the Qwen3-Coder 30B its report label names).

## Scripts

### Local model benchmarks

| Script | What it does |
|---|---|
| `benchmark-local-models.py` | Run a single coding task (FizzBuzz, calculator, etc.) against multiple local-preset combinations and report pass/fail, iterations, wall time, quality score. Fast sanity check. |
| `run_model_benchmark.py` | Local pipeline runs of each model in its `MODELS` list (currently only Qwen3-Coder 30B; 4 models for the published results) on 3 tasks (FizzBuzz / FastAPI / Chess). Overwrites `docs/model-benchmark-results.md`. |
| `full_pipeline_validation.py` | Full end-to-end sweep across all 4 local models on both SWE-bench (3 simple instances) and standard tasks (FizzBuzz / FastAPI). The "honest capabilities" data source. |

### SWE-bench evaluation

| Script | What it does |
|---|---|
| `run_swebench_test.py` | Single SWE-bench instance on each of cloud (Claude) and local — fast smoke test. |
| `benchmark_swebench_architects.py` | Hold implementer + reviewer fixed; vary the architect across 4 local models on 3 SWE-bench Lite simple instances (12 runs). |
| `test_architect_plans.py` | Lightweight plan-quality check — runs only the architect stage against 3 SWE-bench bugs across 4 local models. No implementer, no sandbox. Fast. |

### GPT-OSS supplementary tests

| Script | What it does |
|---|---|
| `test_gpt_oss_architect.py` | Does `gpt-oss:20b` produce relevant architect plans? Isolated test — doesn't re-run the main 4. |
| `test_gpt_oss_20b.py` | Broader gpt-oss:20b sweep: 3 SWE-bench instances + 2 standard tasks, as architect+implementer. |

## Running

```bash
# Fast smoke test — one task, multiple local presets
python benchmarks/benchmark-local-models.py --task fizzbuzz

# Plan-only evaluation (no sandbox, minutes not hours)
python benchmarks/test_architect_plans.py

# Full sweep (multi-hour, writes results to docs/)
python benchmarks/run_model_benchmark.py
python benchmarks/full_pipeline_validation.py
```

`run_model_benchmark.py`, `run_swebench_test.py`, `benchmark_swebench_architects.py`, `test_architect_plans.py` and `full_pipeline_validation.py` overwrite their report under `docs/` (`model-benchmark-results.md`, `swebench-results.md`, `swebench-architect-benchmark.md`, `architect-plan-test.md`, `full-pipeline-validation.md`); the rest print to stdout or write a JSON file. `full_pipeline_validation.py` and `test_gpt_oss_20b.py` write their results to `validation-results/` and their logs to `logs/` in the repository root. `run_model_benchmark.py` creates its scratch workspaces under `~/delegatron-workspace/model-benchmark` (override with `DELEGATRON_BENCHMARK_WORKSPACE`). Check each script for the exact output location.
