# Capabilities and Limitations

Real-world test results from actual Delegatron runs.
Every claim is backed by a completed pipeline run with recorded artifacts.

Last updated: 2026-04-17 (test results). Descriptions of current behaviour
re-checked against the code on 2026-10-14.

---

## What Delegatron Is (and Isn't)

### Delegatron is: **"From prompt to working project in minutes"**

A scaffolding pipeline for **generating new code from scratch** — REST APIs,
CLI tools, microservices, utility modules, test suites. You give it a task
description; it returns a working repo with tests.

**Use cases it serves well:**
- Hackathon projects, prototypes
- Microservice scaffolding
- Template-based project generation
- Learning exercises ("show me a FastAPI + SQLite app with tests")
- Generating test suites for existing code
- Multi-milestone projects (validated 2026-04-17 with local preset)
- Polyglot projects (Python, JS/TS, Go, Rust, Java, PHP all validated E2E)

**Use cases it does NOT serve:**
- Fixing bugs in existing large codebases (use Aider, Cursor, or OpenHands)
- SWE-bench style tasks (needs agentic tool-use loop we don't have)
- Autonomous multi-turn exploration (single-shot per iteration by design)
- Replacing a developer (it's a boilerplate generator, not a coding assistant)

### Delegatron is NOT Aider / Cursor / Copilot / OpenHands

- **SWE-bench: 0/11 resolutions** across 4 local models (Apr 16 validation)
- Published: Qwen3-Coder 30B scores 50.3% on SWE-bench Verified — **with
  OpenHands' 100-turn agentic scaffolding**. Delegatron's few-iteration
  architect/implement/review loop (5 iterations by default) cannot match that.

If you need to fix real GitHub bugs, use OpenHands, not Delegatron.

---

## What Works (Verified)

### Language sandboxes — all validated E2E with local preset

| Language | Image | Validation |
|---|---|---|
| Python | `python:3.12-slim` | Baseline — works everywhere. `fib.py` / `Stack` class / many other runs |
| JavaScript / TypeScript | `node:22-slim` | React + Vitest + Testing Library validated 2026-04-17. 2/2 tests passed with `local` preset + framework hints |
| Go | `golang:1.23-bookworm` | `fib.go` + `fib_test.go`, 2 iter APPROVED, $0.0049 |
| Rust | `rust:1.82-slim-bookworm` | `lib.rs` with 4/4 `#[test]` functions, 2 iter APPROVED, $0.0054 |
| Java | `maven:3.9-eclipse-temurin-21` | JUnit 5, 4/4 tests, 2 iter APPROVED, $0.0053 |
| PHP | `composer:2` | PHPUnit, 4/4 tests, 4/4 assertions, 2 iter APPROVED, $0.0059 |
| C# / .NET | `mcr.microsoft.com/dotnet/sdk:9.0` | Image configured, no recorded E2E |
| Ruby | `ruby:3.3-slim` | Image configured, no recorded E2E |
| C / C++ (CMake, Makefile) | `gcc:14` | Image configured, no recorded E2E |
| PowerShell (Pester) | `mcr.microsoft.com/powershell:7.4-ubuntu-22.04` | Image configured, no recorded E2E |

Image auto-selection happens from `package.json` / `go.mod` / `Cargo.toml` /
`pom.xml` / `composer.json` / etc.  Explicit `--docker-image` still wins
when you want to override.

### Cloud — Claude Sonnet (`single-claude` preset)

| Task | Result | Cost | Iterations |
|---|---|---|---|
| FastAPI CRUD + sqlite3 + tests | **APPROVED 13/13** | $0.10 | 1 |
| Node.js Express + Jest | **APPROVED 12/12** | $0.08 | 1 |
| Chess engine + GUI | Stalled 40/41 | $0.43 | 3 |

Cloud presets reliably produce working code for standard backend tasks.
Typical cost: $0.08 - $0.50 per run.

### Local — Planning phase (all 4 models)

All 4 local models produce **relevant, on-topic plans** for real SWE-bench
bugs when Ollama has a clean state:

| Architect | On-topic | Avg Time | Avg Tokens |
|---|---|---|---|
| Qwen 3.5 9B | 3/3 | 93s | 850 |
| Phi-4 14B | 3/3 | 40s | 494 |
| Qwen2.5-Coder 14B | 3/3 | 46s | 365 |
| **Qwen3-Coder 30B** | **3/3** | **31s** ⚡ | 407 |

See [architect-plan-test.md](architect-plan-test.md) for details.

### Local — Simple tasks (real E2E runs against a test repository)

| Task | Preset | Result | Cost | Iter |
|---|---|---|---|---|
| Fibonacci + pytest | `local` | **APPROVED 5/5** | $0.0030 | 2 |
| Stack class + pytest | `local` | **APPROVED 5/5** | $0.0047 | 2 |
| React Counter + Vitest | `local` | **APPROVED 2/2** | $0.0062 | 3 |
| Multi-milestone (fib + factorial + is_prime) | `local` + fan-out | **APPROVED 12/12** across 3 modules | $0.0057 | 3 waves |
| FizzBuzz | Phi-4 14B | **5/5 tests passed** | — | 1 |

### GitHub PR creation — real end-to-end

Validated 2026-04-17 against a private test repository:
- PR #1 (closed): fib.py + tests, full audit body
- PR #2 (closed): sum_list + tests, 17 additions with no artifact bloat
- PR #3 (closed): fib again, sanity check
- PR #4 (closed): Stack class + tests

Every PR had the full audit summary (request, plan, cost per role, review
verdict, acceptance criteria).  Graceful skip when `gh` CLI is missing,
run status isn't `approved`, or blocking security findings remain.  Never
fails the run on PR error.

### Infrastructure

- **Docker sandbox:** All languages with a recorded validation above
  confirmed working end-to-end
- **Docker named volumes:** JS `node_modules`, PHP `vendor` persist across
  iterations (fixed 2026-04-17 — old symlink approach broke on WSL2)
- **Docker container caching:** Skips repeated `pip install` / `npm install`
- **Internal directories kept out of git:** Delegatron's own directories
  (`runs/`, `reviews/`, `_sandbox/`, `.delegatron-runs/`,
  `.delegatron-worktrees/`, `.delegatron-venv/`) get entries in
  `.git/info/exclude` anchored to the workspace root, so a nested user
  `src/runs/` stays visible and the repository's `.gitignore` is never edited
  or committed. `runs/` and `reviews/` count only when they hold Delegatron's
  own run or review layout, and a directory the repo already tracks files in
  is left alone.
- **Dashboard API:** 35+ endpoints; `/api/runs` now accepts
  `create_pr` / `pr_base` / `pr_draft` for PR creation from the UI
- **CLI:** `run`, `plan`, `review`, `refine`, `resume`, `retry`, `show`,
  `list-runs`, `list-presets`, `estimate`, `project`, `template`
  (list/run/save/delete), `benchmark`, `swebench`, `serve` — all functional
- **30 built-in templates** with auto-load, search, built-in/user
  differentiation, variable listing in the `/templates` page
- **Tests:** 1421+ unit tests, 27 Ollama integration tests, 0 lint errors,
  0 type errors (2026-04-17; 3100+ unit test functions as of 2026-10-14)

---

## What Fails (Verified)

### SWE-bench — All local models

**Tested Apr 16 with fresh Ollama, all fixes applied:**

| Implementer | Resolved | Notes |
|---|---|---|
| Qwen 3.5 9B | 0/2 | Both timeouts (15 min) |
| Phi-4 14B | 0/3 | 1 exhausted, 1 error, 1 timeout |
| Qwen2.5-Coder 14B | 0/3 | All exhausted |
| Qwen3-Coder 30B | 0/2 | Both timeouts |
| **Total** | **0/11** | — |

Architects produced correct plans. Implementers couldn't write working
patches. Delegatron's pipeline shape (3 iterations, no tool-use loop,
pre-gathered context) doesn't match SWE-bench's requirements.

### Chess engine — All local models

19+ runs across different local presets. 0 approvals. Too complex for
14B/30B local models in 3-iteration budgets.

Cloud models (Claude Sonnet) get 40/41 tests — close but not full approval.

### Ambitious templates — local models

The `cli-tool` template asks for subcommands + Rich + progress bars + JSON
mode + config file in one run. EXHAUSTED with both `local` and `local-heavy`
presets (14B and 30B).

Interpretation: this isn't a model regression — it's a template design
question.  Either trim scope or split into multiple milestones via Project
Mode.  Cloud models probably satisfy the full template; local models can't.

---

## Hardware Boundaries

### RTX 4070 Ti / 12 GB VRAM (primary test card)

| Preset | Fits in VRAM | Speed | Reliability |
|---|---|---|---|
| `local` (phi4 + qwen2.5-coder:14b + phi4) | ✅ Yes | ~50 tok/s | ✅ Reliable |
| `local-phi4` (all phi4:14b) | ✅ Yes | ~50 tok/s | ✅ Reliable, fastest |
| `local-heavy` (qwen3-coder:30b impl) | ❌ No — 35 GB spills to RAM | ~10 tok/s | ⚠️ Unreliable, exhausts |

**⚠️ `local-heavy` is NOT recommended on cards under 24 GB VRAM.**
Qwen3-Coder 30B has 45 GB of weights (MoE, 3.3B active per token).  On a
12 GB card only ~10 GB fits in VRAM; the remaining 35 GB lives in system
RAM and gets swapped over PCIe per token.  Result: 3× slower inference
and exhausted iteration budgets on anything non-trivial.  Only useful on
cards that can hold the full model in VRAM.

**What local models can do on 12 GB:** FizzBuzz, basic APIs, utility
modules, single-file scripts, test generation, simple React components,
Go/Rust/Java/PHP/Python boilerplate.

**What they can't do:** SWE-bench bugs, chess, multi-file refactoring in
existing codebases, ambitious "one-shot build-everything" template tasks.

---

## Pipeline Behavior

- **Stuck detection:** A task whose reviews keep raising the same issues
  ends `stalled`; blocker/major issues the reviewer attributes to the plan
  (or a director ruling) trigger a re-plan, at most `max_replans` (default 2)
  times per run.  Other patterns such as alternating failures are logged as
  advisory `stuck_detected` events.  Doesn't help if the model
  fundamentally can't solve the task.
- **JSON repair:** Handles `<think>` blocks, markdown fences, malformed
  JSON.  Works for formatting issues, not structural ones.
- **Patch application:** Supports whole-file `files`, search/replace
  `edits`, `delete_files` and unified diffs.  Unsafe paths (absolute, `..`,
  inside `.git`) are never written and go back to the implementer as
  blockers.  Non-git workspaces get their files written directly (fixed
  2026-04-17 — previously silently false-failed).
- **Framework hints:** Implementer prompt now includes project-specific
  hints (e.g. "This project uses Vitest, NOT Jest.  Use `fireEvent`, not
  `user-event`").  Caught 14B models' Jest/Vitest API mixing.
- **Import validator:** Cross-checks every patch's imports against the
  project's manifests. Imports nothing declares become advisory import
  warnings: shown to the reviewer, and to the implementer after a failed test
  run. They never block approval on their own. Recognised sources:
  - JavaScript/TypeScript: `package.json` dependencies (all four sections),
    Node built-ins with or without the `node:` scheme, `@/` and `~/` path
    aliases, and the `compilerOptions.paths` aliases of a root
    `tsconfig.json` / `jsconfig.json` (comments and trailing commas allowed)
  - Python: `pyproject.toml` (PEP 621 dependencies and extras, PEP 735
    `[dependency-groups]`, Poetry main/dev/`group.*` tables, PDM and uv dev
    dependencies), `setup.cfg`, and root `requirements*.txt` /
    `requirements/*.txt` with their `-r` includes; stdlib and local modules
  - Rust: `Cargo.toml` `[dependencies]`, `[dev-dependencies]` and
    `[build-dependencies]`, including `[target.<cfg>.*]` tables; std crates,
    `mod` items and types or enum variants used as a `use` root
  - Go: `go.mod` requires plus the standard library; Java: best-effort
    against `pom.xml`

---

## Cost Reference

| Preset | Typical Run Cost | Wall Time |
|---|---|---|
| default (Claude + GPT + Gemini) | $0.30 - $0.70 | 3-10 min |
| single-claude (Claude Sonnet all roles) | $0.08 - $0.50 | 3-10 min |
| claude-heavy (Opus + Sonnet + Gemini) | $0.50 - $2.00 | 3-10 min |
| budget (Gemini Flash) | $0.01 - $0.10 | 3-10 min |
| local / local-phi4 | $0.00 (electricity) | 3-15 min |
| local-heavy (24 GB+ GPU) | $0.00 (electricity) | 10-30 min |
| local-heavy (< 24 GB GPU) | $0.00 but slow + unreliable | 30+ min |

---

## Not Yet Validated

- **E2B sandbox** (adapter exists, not tested — requires E2B API key)
- **C# / .NET sandbox** (image configured, no E2E recorded)
- **Ruby sandbox** (image configured, no E2E recorded)
- **C / C++ and PowerShell sandboxes** (images configured, no E2E recorded)
- **Live Kubernetes install of Helm chart** (chart itself passes lint +
  kubeconform strict schema validation, but no cluster install tested)

---

## Recommendations

1. **For cloud-capable tasks:** use `single-claude` or `default`.  Most
   reliable, typical cost $0.08 - $0.50.
2. **For local development on 12 GB VRAM:** use `local` (default) or
   `local-phi4`.  Both fit fully in VRAM.  Use `local` for unfamiliar
   tasks, `local-phi4` when speed matters more than code quality.
3. **For local development on 24 GB+ VRAM:** `local-heavy` is viable if
   qwen3-coder:30b fits in VRAM on your card.
4. **Don't use Delegatron for:** bug fixing in existing codebases,
   SWE-bench tasks, multi-file refactoring.
5. **Before using a template:** Check its plan before committing: preview
   the rendered request with `delegatron plan`, or run it with
   `interactive = true` (`[pipeline]` in `delegatron.toml`, or the template's
   own overrides) — `delegatron template run` has no `--interactive` flag.
6. **If the pipeline produces identical wrong output repeatedly:** restart
   Ollama (Settings → Ollama → Restart Ollama button in the dashboard, or
   `POST /api/ollama/restart`, or `ollama serve` restart manually).
   Stuck KV cache or connection state causes "template collapse" in local
   models.
