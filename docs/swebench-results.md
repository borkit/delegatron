# SWE-bench Evaluation Results

Date: 2026-04-16
Split: lite, 1 instance each

> **Note (2026-10-14):** historical snapshot, not re-measured. On 2026-04-16 the `local` preset ran Qwen3-Coder 30B as implementer; it now runs phi4:14b + qwen2.5-coder:14b + phi4:14b with v5 prompts. `benchmarks/run_swebench_test.py` overwrites this file.

## Results

| Preset | Instance | Resolved | Status | Iterations | Cost | Time |
|--------|----------|----------|--------|-----------|------|------|
| Qwen3-Coder 30B (local) | astropy__astropy-12907 | NO | error | 2 | $0.0027 | 774.1s |
| Claude Sonnet (cloud) | astropy__astropy-12907 | NO | exhausted | 5 | $0.4315 | 329.6s |

## Raw Data

```json
[
  {
    "preset": "local",
    "label": "Qwen3-Coder 30B (local)",
    "total": 1,
    "resolve_rate": 0.0,
    "cost": 0.0027140000000000003,
    "time": 774.1,
    "results": [
      {
        "instance": "astropy__astropy-12907",
        "resolved": false,
        "status": "error",
        "iterations": 2,
        "cost": 0.0027140000000000003,
        "emcs": 0.5
      }
    ]
  },
  {
    "preset": "single-claude",
    "label": "Claude Sonnet (cloud)",
    "total": 1,
    "resolve_rate": 0.0,
    "cost": 0.4314719999999999,
    "time": 329.6,
    "results": [
      {
        "instance": "astropy__astropy-12907",
        "resolved": false,
        "status": "exhausted",
        "iterations": 5,
        "cost": 0.4314719999999999,
        "emcs": 1.0
      }
    ]
  }
]
```