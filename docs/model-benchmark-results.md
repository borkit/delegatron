# Local Model Benchmark Results

**Date:** 2026-04-16
**Hardware:** Local GPU (Ollama), ~188W TDP during generation
**Configuration:** qwen3.5:9b as architect/reviewer, v4 prompts, Docker sandbox (python:3.12-slim), network isolation enabled
**Budget:** max_iterations varies by run (3 for smaller models, 6 for Qwen3-Coder chess)

> **Note (2026-10-14):** historical snapshot, not re-measured. `local-all` is not a current preset; the local presets now use phi4:14b as architect and reviewer with v5 prompts, and `max_iterations` is a budget per task. The pipeline has since gained a pre-run test baseline, per-task stall handling (a stalled task is skipped), director arbitration and the `partial` run status, so these results do not describe current runs. `benchmarks/run_model_benchmark.py` overwrites this file.

## Results Matrix

| Model | Size | FizzBuzz (Easy) | FastAPI CRUD (Medium) | Chess Engine (Hard) |
|-------|------|-----------------|----------------------|---------------------|
| Phi-4 | 14B | 5/5 tests, exhausted, 581s | 0/3 tests, exhausted, 881s | 0 tests (no tests ran), exhausted, 1283s |
| Gemma 3 | 12B | 3/5 tests, exhausted, 520s | 0 tests, exhausted, 510s | 0/3 tests, exhausted, 634s |
| Qwen2.5-Coder | 14B | 4/5 tests, exhausted, 398s | 6/8 tests, exhausted, 539s | 0/3 tests, exhausted, 1121s |
| **Qwen3-Coder** | **30B** | **2/5 tests, exhausted, 577s** | **0/3 tests, exhausted, 775s** | **4/4 smoke only, exhausted, 2657s** |
| Claude Sonnet (cloud) | -- | N/A | 13/13 APPROVED, 1 iter, $0.10 | 40/41 stalled, 3 iter, $0.43 |

**Legend:** All local models ran the full `local-all` preset (same model for all three roles). Cloud baseline used `single-claude` preset. "Exhausted" means the pipeline hit max iterations without approval. Tests passed shown as best result across all iterations.

## Detailed Results by Model

### Phi-4 14B

| Task | Tests | Status | Iterations | Wall Time | Notes |
|------|-------|--------|------------|-----------|-------|
| FizzBuzz | 5/5 passed | Exhausted | 3 | 581s | All tests passing but reviewer never approved |
| FastAPI CRUD | 0/3 passed | Exhausted | 3 | 881s | Could not produce working CRUD endpoints |
| Chess Engine | No tests ran | Exhausted | 3 | 1283s | Failed to produce testable code |

**Summary:** Phi-4 was the only model to pass all 5 FizzBuzz tests, but could not handle medium or hard tasks. Slowest on FizzBuzz among the smaller models. The chess task produced no runnable tests at all, indicating the model could not follow the multi-file task specification.

### Gemma 3 12B

| Task | Tests | Status | Iterations | Wall Time | Notes |
|------|-------|--------|------------|-----------|-------|
| FizzBuzz | 3/5 passed | Exhausted | 3 | 520s | Partial correctness |
| FastAPI CRUD | 0 tests | Exhausted | 3 | 510s | Tests could not be collected |
| Chess Engine | 0/3 passed | Exhausted | 3 | 634s | No tests passing |

**Summary:** Gemma 3 was fastest on FastAPI (510s) but produced no working test results for medium or hard tasks. On FizzBuzz it achieved partial correctness with 3/5 tests. Smallest model tested at 12B parameters.

### Qwen2.5-Coder 14B

| Task | Tests | Status | Iterations | Wall Time | Notes |
|------|-------|--------|------------|-----------|-------|
| FizzBuzz | 4/5 passed | Exhausted | 3 | 398s | Close to full correctness |
| FastAPI CRUD | 6/8 passed | Exhausted | 3 | 539s | Best local result on medium task |
| Chess Engine | 0/3 passed | Exhausted | 3 | 1121s | No chess tests passing |

**Summary:** Qwen2.5-Coder was the strongest performer among 14B-class models. It was the fastest on FizzBuzz (398s), came close to full correctness (4/5), and was the only local model to achieve meaningful test passage on the FastAPI task (6/8 tests). It still could not handle the chess engine task. Best overall speed-to-quality ratio of the smaller models.

### Qwen3-Coder 30B

| Task | Tests | Status | Iterations | Wall Time | Cost (local) | Tokens |
|------|-------|--------|------------|-----------|--------------|--------|
| FizzBuzz | 2/5 passed (best: iter 3) | Exhausted | 3 | 577s | $0.0097 | 19.6K in / 6.7K out |
| FastAPI CRUD | 0/3 passed | Exhausted | 3 | 775s | $0.0131 | 17.2K in / 9.9K out |
| Chess Engine | 4/4 smoke tests only | Exhausted | 6 | 2657s | $0.0611 | 45.5K in / 38.6K out |

**FizzBuzz detail:** The model produced a correct FizzBuzz implementation but generated tests with incorrect expected values. Iteration 1 had 0/5 passing, iteration 2 had 0/5 passing, iteration 3 improved to 2/5. The core bug was tests expecting non-FizzBuzz values for numbers like 3 (expected '3' instead of 'Fizz'), indicating the tests were wrong rather than the implementation.

**FastAPI CRUD detail:** All three iterations failed at test collection. Iteration 1 produced a FastAPIError due to invalid response model typing (using `typing.List[User]` with a non-Pydantic model). Iteration 2 failed with a missing `httpx` module (not included in requirements.txt). Iteration 3 repeated the FastAPIError. The model could not resolve the Pydantic/FastAPI type compatibility issue across iterations.

**Chess Engine detail:** This was the most resource-intensive run. The model was given 6 iterations (double the standard 3). Progression:
- Iteration 1: 9/11 tests passing (2 failures: pawn capture + turn enforcement)
- Iteration 2: 8/10 passing (but still stuck on same issues)
- Iteration 3: 4/11 passing (regression -- rewrote code with inverted piece colors)
- Iteration 4: 7/14 passing (added more tests, but core move validation broken)
- Iteration 5: 3/11 passing (further regression)
- Iteration 6: 4/4 passing (but model deleted functional tests, only smoke tests remained)

The model hit the "alternating pattern" stuck detection starting at iteration 3. Its final "fix" was to replace functional tests with smoke tests (file exists + valid syntax), which technically passed but did not validate correctness. The reviewer caught this but the run was already exhausted. The root bug was inverting uppercase=white vs uppercase=black conventions between the engine and tests.

**Notable observations:**
- Zero-cloud verification passed on all three runs (no data left the local network)
- GPU power draw averaged ~188W during generation for the 30B model
- Local cost breakdown: ~75% hardware amortization, ~25% electricity
- Stall detection triggered: 1x on FizzBuzz, 1x on FastAPI, 4x on Chess

### Cloud Baseline: Claude Sonnet (`single-claude` preset)

| Task | Tests | Status | Iterations | Cost | EMCS |
|------|-------|--------|------------|------|------|
| FastAPI CRUD | 13/13 passed | APPROVED | 1 | $0.10 | 1.0 |
| Node.js CLI | 12/12 passed | APPROVED | 1 | $0.08 | 1.0 |
| Chess Engine | 40/41 passed | Stalled | 3 | $0.43 | 0.75 |

**Summary:** Cloud models solved easy and medium tasks in a single iteration with perfect scores. Even on the hard chess task, Claude achieved 40/41 tests (97.6% pass rate) and EMCS 0.75, though it stalled trying to fix the last test.

## Key Findings

### 1. No local model achieved pipeline approval on any task

Every local model run terminated with "exhausted" status. Even when tests passed (Phi-4 FizzBuzz: 5/5), the review loop did not converge to approval within the iteration budget. This indicates the architect/reviewer (qwen3.5:9b) may be contributing to non-convergence -- it sometimes rewrites tests with incorrect expectations or fails to provide actionable review feedback.

### 2. Model size does not linearly predict quality

Qwen3-Coder 30B (the largest model) performed worse than Qwen2.5-Coder 14B on both FizzBuzz (2/5 vs 4/5) and FastAPI (0/3 vs 6/8). The 30B model was also 1.4-2x slower per task. Larger models may over-complicate solutions or be more prone to the "rewrite everything" anti-pattern when iterating on feedback.

### 3. The chess task exposes fundamental self-correction limitations

No local model passed more than basic smoke tests on the chess task. The common failure pattern was:
- Iteration 1 gets close (Qwen3-Coder hit 9/11)
- Subsequent iterations introduce regressions by rewriting working code
- Models cannot isolate the specific bug and instead perform whole-file rewrites
- The "alternating pattern" stuck detection fires repeatedly

### 4. Test-implementation mismatch is the dominant failure mode

Across all models and tasks, the most common issue was models generating tests whose expectations did not match the implementation. For FizzBuzz, models would write correct `fizzbuzz()` functions but tests expecting `'3'` instead of `'Fizz'` for `fizzbuzz(4)[2]`. For FastAPI, models would use `typing.List[User]` with dataclasses instead of Pydantic models.

### 5. Cloud models are 50-100x more cost-effective per test passed

| Metric | Best Local (Qwen2.5-Coder 14B) | Cloud (Claude Sonnet) |
|--------|--------------------------------|----------------------|
| FastAPI tests passed | 6/8 | 13/13 |
| FastAPI cost | ~$0.01 (electricity+hardware) | $0.10 |
| FastAPI time | 539s | ~60s |
| Cost per test passed | ~$0.002 | ~$0.008 |
| Time per test passed | ~90s | ~5s |

While local models are cheaper per run, they achieve far fewer test passes. When measured as cost-per-successful-test or time-to-working-code, cloud models dominate.

### 6. Iteration count matters less than first-attempt quality

Giving Qwen3-Coder 6 iterations on chess (instead of 3) did not improve outcomes -- it made them worse through regressions. The models lack the ability to diagnose specific failures from test output and make targeted fixes. More iterations just mean more opportunities to break working code.

## Recommendations

### Use cloud models for production work
Claude Sonnet achieves perfect or near-perfect results in 1-3 iterations for $0.08-$0.43 per task. The time savings alone (60s vs 500-2600s) justify the cost for any task where developer time matters.

### Use Qwen2.5-Coder 14B for local development/testing
Among local models tested, Qwen2.5-Coder 14B offers the best balance of speed (fastest), quality (6/8 on FastAPI, 4/5 on FizzBuzz), and resource efficiency. It outperformed the 30B Qwen3-Coder despite being less than half the size.

### Limit local model iterations to 2-3
More iterations cause regressions rather than improvements. Set `max_iterations: 3` for local models and invest in better first-attempt prompts rather than relying on the correction loop.

### Investigate reviewer quality for local runs
The qwen3.5:9b reviewer may be part of the problem. It sometimes accepts code that fails tests (iteration 2 of Qwen3-Coder chess: reviewer accepted with EMCS 1.0 despite 2/10 test failures) and sometimes provides non-actionable feedback. Testing with a cloud reviewer and local implementer could isolate whether the bottleneck is generation or evaluation.

### Do not use for hard/algorithmic tasks
No local model could produce a working chess engine. Reserve local models for simple generation tasks (boilerplate, CRUD, utilities) and use cloud models for anything requiring algorithmic reasoning, multi-file coordination, or complex state management.

## Test Environment

- **OS:** Windows 11 Pro
- **GPU:** Local (Ollama), ~188W during generation
- **Sandbox:** Docker (python:3.12-slim), network-isolated
- **Delegatron version:** main branch as of 2026-04-16
- **Prompt version:** v4 (optimized for local models)
- **Architect/Reviewer model:** qwen3.5:9b (all local runs)
- **Electricity rate:** $0.12/kWh
- **Hardware amortization:** $1500 GPU / 20000h lifetime
