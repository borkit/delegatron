# Full Pipeline Validation Results

**Date:** 2026-04-16 20:12
**Architect:** `qwen3-coder:30b` (fixed for all runs)
**Reviewer:** `qwen3.5:9b` (fixed for all runs)
**Test timeout:** 900s per instance

## Phase 1: SWE-bench (3 simple instances)

| Implementer | Resolved | Errors | Avg Iter | Avg Time |
|-------------|----------|--------|----------|----------|
| Qwen 3.5 9B | 0/2 | 2/2 | 0.0 | 904s |
| Phi-4 14B | 0/3 | 2/3 | 1.7 | 631s |
| Qwen2.5-Coder 14B | 0/3 | 1/3 | 2.0 | 754s |
| Qwen3-Coder 30B | 0/2 | 2/2 | 0.0 | 904s |

## Phase 2: Standard tasks

| Implementer | FizzBuzz | FastAPI CRUD |
|-------------|----------|--------------|
| Qwen 3.5 9B | error (0s) | error (0s) |
| Phi-4 14B | 3/3 (exhausted) | timeout (900s) |
| Qwen2.5-Coder 14B | error (0s) | error (0s) |
| Qwen3-Coder 30B | 5/5 (exhausted) | timeout (900s) |

## Detailed Results

### Phase 1: SWE-bench

**Qwen 3.5 9B** (`qwen3.5:9b`)
- `pytest-dev__pytest-6116`: resolved=False status=timeout iter=0 time=902s emcs=0.00 error=exceeded 900s
- `django__django-14534`: resolved=False status=timeout iter=0 time=907s emcs=0.00 error=exceeded 900s

**Phi-4 14B** (`phi4:14b`)
- `pytest-dev__pytest-6116`: resolved=False status=exhausted iter=3 time=655s emcs=0.25
- `django__django-14534`: resolved=False status=error iter=2 time=334s emcs=0.70
- `sympy__sympy-18057`: resolved=False status=timeout iter=0 time=905s emcs=0.00 error=exceeded 900s

**Qwen2.5-Coder 14B** (`qwen2.5-coder:14b`)
- `pytest-dev__pytest-6116`: resolved=False status=timeout iter=0 time=902s emcs=0.00 error=exceeded 900s
- `django__django-14534`: resolved=False status=exhausted iter=3 time=616s emcs=0.70
- `sympy__sympy-18057`: resolved=False status=exhausted iter=3 time=743s emcs=0.00

**Qwen3-Coder 30B** (`qwen3-coder:30b`)
- `pytest-dev__pytest-6116`: resolved=False status=timeout iter=0 time=902s emcs=0.00 error=exceeded 900s
- `django__django-14534`: resolved=False status=timeout iter=0 time=907s emcs=0.00 error=exceeded 900s

### Phase 2: Standard Tasks

**Qwen 3.5 9B** (`qwen3.5:9b`)
- FizzBuzz: status=error tests=0/0 emcs=0.00 time=0s error=Invalid run_id: 'val-std-fizzbuzz-qwen3.5-9b'
- FastAPI CRUD: status=error tests=0/0 emcs=0.00 time=0s error=Invalid run_id: 'val-std-fastapi-qwen3.5-9b'

**Phi-4 14B** (`phi4:14b`)
- FizzBuzz: status=exhausted tests=3/3 emcs=0.00 time=442s
- FastAPI CRUD: status=timeout tests=0/0 emcs=0.00 time=900s error=exceeded 900s

**Qwen2.5-Coder 14B** (`qwen2.5-coder:14b`)
- FizzBuzz: status=error tests=0/0 emcs=0.00 time=0s error=Invalid run_id: 'val-std-fizzbuzz-qwen2.5-coder-14b'
- FastAPI CRUD: status=error tests=0/0 emcs=0.00 time=0s error=Invalid run_id: 'val-std-fastapi-qwen2.5-coder-14b'

**Qwen3-Coder 30B** (`qwen3-coder:30b`)
- FizzBuzz: status=exhausted tests=5/5 emcs=0.00 time=496s
- FastAPI CRUD: status=timeout tests=0/0 emcs=0.00 time=900s error=exceeded 900s