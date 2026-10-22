# Architect Plan Quality Test

**Date:** 2026-04-16

Tests each local model as architect against 3 real SWE-bench bugs.
Pure planning test — no implementer, no sandbox, no Docker.
**Just asks: given a real GitHub bug report, does the model produce a relevant plan?**

## TL;DR

**All 4 local models produce correct, on-topic plans for real SWE-bench bugs.**

The "template collapse" we observed earlier (qwen3.5:9b producing identical
"CSV mean calculator" plans for every bug) was caused by **stuck Ollama state**,
not model limitations. A clean Ollama restart fixed it.

## Setup

- Ollama was freshly restarted before the test
- Architects called directly via `OpenAIProvider` (not through Pipeline)
- Used the v4 architect prompt (no chess example)
- Simplified problem statement prepended: "Fix this bug in the {repo} repository..."
- No file_tree or project_info passed to architect (keeps context small)

## Test Instances (3 easiest SWE-bench Lite)

1. `pytest-dev__pytest-6116` — Add short `-c` CLI option for `--collect-only`
2. `django__django-14534` — Fix `BoundWidget.id_for_label` ignoring widget id
3. `sympy__sympy-18057` — Fix `__eq__` calling `sympify` on unknown object reprs

## Results Matrix

| Architect | On-topic | Template | Avg Time | Avg Tokens | Rank |
|-----------|----------|----------|----------|------------|------|
| Qwen 3.5 9B | **3/3** | 0/3 | 93.1s | 850 | 4th (slowest) |
| Phi-4 14B | **3/3** | 0/3 | 39.9s | 494 | 2nd |
| Qwen2.5-Coder 14B | **3/3** | 0/3 | 46.1s | 365 | 3rd |
| **Qwen3-Coder 30B** | **3/3** | 0/3 | **30.8s** | 407 | **1st (fastest + correct)** |

## Winner: Qwen3-Coder 30B

Counter-intuitive finding: the **largest** model was also the **fastest**.
Explanation: qwen3-coder:30b is a Mixture-of-Experts (MoE) with only 3.3B
active parameters per token. It's big on disk (18GB) but fast in practice.

The 30B also produces the **cleanest, most technically precise plans** — e.g.
for the sympy bug it correctly identified the security implication:
> "Fix the bug in SymPy's `__eq__` method where it incorrectly attempts to
> evaluate reprs of unknown objects, which can lead to security vulnerabilities"

## Implications

1. **Local models CAN plan real bug fixes** — our earlier "local models can't
   do SWE-bench" conclusion was wrong (at least for planning)
2. **The bottleneck is elsewhere** — likely the implementer step (writing the
   actual fix) or the full pipeline's context/iteration behavior
3. **Ollama state matters** — stuck KV cache or connection state causes
   template collapse. Restart Ollama if you see repeated identical outputs.
4. **MoE models are underrated** — qwen3-coder:30b (MoE) beats both 14B dense
   models on speed AND quality for planning

## What This Test Does NOT Prove

- Whether any of these models can actually produce a **working patch** for
  these bugs (that requires implementer + sandbox)
- Whether the full Delegatron pipeline with a local architect will resolve
  SWE-bench instances end-to-end (likely still 0% — implementer is the hard part)
- Plan quality in more complex bug reports (these 3 are the simplest tier)

## Detailed Plans

### Qwen 3.5 9B (`qwen3.5:9b`)

- **pytest-dev__pytest-6116** (110.2s, 962 tokens) ✓
  - Plan: Add a one-character short option (e.g., -c) for the existing --collect-only CLI argument in pytest
- **django__django-14534** (92.8s, 872 tokens) ✓
  - Plan: Fix BoundWidget.id_for_label() to use self.data['attrs']['id'] instead of generating a new ID, resolving the bug where CheckboxSelectMultiple label IDs are incorrectly generated when auto_id is customized
- **sympy__sympy-18057** (76.4s, 717 tokens) ✓
  - Plan: Fix the sympy bug where __eq__ incorrectly calls sympify on unknown objects whose repr looks like sympy code

### Phi-4 14B (`phi4:14b`)

- **pytest-dev__pytest-6116** (40.3s, 459 tokens) ✓
  - Plan: Implement a one-character shortcut for the `--collect-only` command in pytest.
- **django__django-14534** (34.6s, 450 tokens) ✓
  - Plan: Fix the bug in django/django where BoundWidget.id_for_label ignores the 'id' set by ChoiceWidget.options for CheckboxSelectMultiple widgets.
- **sympy__sympy-18057** (44.8s, 572 tokens) ✓
  - Plan: Fix the bug in SymPy's `__eq__` method to prevent unsafe evaluation of string representations.

### Qwen2.5-Coder 14B (`qwen2.5-coder:14b`)

- **pytest-dev__pytest-6116** (67.3s, 506 tokens) ✓
  - Plan: Add a one-character shortcut for the `--collect-only` command in pytest.
- **django__django-14534** (29.7s, 250 tokens) ✓
  - Plan: Fix bug in BoundWidget.id_for_label to respect id set by ChoiceWidget.options
- **sympy__sympy-18057** (41.4s, 338 tokens) ✓
  - Plan: Fix the bug in SymPy's __eq__ method to prevent eval of unknown object reprs.

### Qwen3-Coder 30B (`qwen3-coder:30b`)

- **pytest-dev__pytest-6116** (45.5s, 442 tokens) ✓
  - Plan: Add a one-character shortcut command for the --collect-only pytest CLI argument
- **django__django-14534** (20.1s, 390 tokens) ✓
  - Plan: Fix BoundWidget.id_for_label() to use the id from widget attributes instead of generating a default id
- **sympy__sympy-18057** (26.8s, 390 tokens) ✓
  - Plan: Fix the bug in SymPy's __eq__ method where it incorrectly attempts to evaluate reprs of unknown objects, which can lead to security vulnerabilities and unexpected behavior.

## Addendum — GPT-OSS 20B (2026-04-16)

Ran `benchmarks/test_gpt_oss_architect.py` against a different triple of
SWE-bench Lite "simple" instances (sorted by problem statement length):

| Instance | Time | On-topic? |
|----------|------|-----------|
| `pytest-dev__pytest-5221` | 119.3s | ✓ |
| `sympy__sympy-16503` | 380.9s | ✓ |
| `sympy__sympy-15345` | 269.5s | ✓ (keyword metric flagged 1/10, manual review confirms plan matches the Mathematica-code-generation bug) |

**Summary:** 3/3 manually on-topic, avg 256.6s per plan.

GPT-OSS 20B produces coherent plans but is **3–8× slower than Phi-4 14B**
and slower than Qwen3-Coder 30B MoE despite being smaller. Not promoted to
a default preset — phi4 wins on speed × quality.

## Recommendations (updated 2026-04-17)

For local pipeline runs:
1. **Use `local` preset** — phi4 architect + qwen2.5-coder:14b implementer
   + phi4 reviewer.  Phi-4 is empirically ~4× faster than qwen3.5:9b with
   better review quality.  Both models fit fully in 12 GB VRAM.
2. **Use `local-phi4`** for the fastest single-model runs on simple tasks
   (phi4:14b in all roles).  Also fits 12 GB.
3. **`local-heavy` requires 24 GB+ VRAM.**  The 30B implementer spills
   30+ GB to system RAM on smaller cards, runs 3× slower per token, and
   exhausts the iteration budget.  Only useful when the whole model fits
   in GPU memory.
4. **Restart Ollama** (Settings → Ollama → "Restart Ollama" button, or
   `POST /api/ollama/restart`, admin role when auth is enabled) if you see
   repeated identical wrong output — that's "template collapse" from stuck
   KV cache.
5. **Don't pass large file trees to the architect** — it hallucinates when
   overwhelmed.
6. **Accept that planning ≠ resolution** — a good plan doesn't guarantee
   a working patch; SWE-bench still resolves at 0% locally (see
   `docs/swebench-results.md`).

> **Note (2026-10):** since this test, the `local-oss` (gpt-oss:20b
> implementer) and `local-oss-review` (gpt-oss:20b reviewer) presets were
> added (validated 2026-04-18), and every local preset now pins prompt v5
> for all three roles; this test used the v4 architect prompt. See
> `delegatron list-presets`.

## Reproducing

```bash
# Restart Ollama first (from dashboard or CLI)
# Then:
python benchmarks/test_architect_plans.py       # 4-model baseline
python benchmarks/test_gpt_oss_architect.py     # GPT-OSS 20B only
# test_architect_plans.py regenerates this doc (overwriting the addendum and
# recommendations); test_gpt_oss_architect.py writes
# logs/gpt-oss-architect-results.json.
```
