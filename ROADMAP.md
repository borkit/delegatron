# Delegatron Roadmap

## Known Unvalidated Features (as of 2026-04-17)

Features with code but no end-to-end verification. Hidden from UI until validated.

- **E2B sandbox** — adapter exists, not tested (requires E2B API key)

### Recently Validated

- **Local model benchmark sweep** — 2026-04-18. 6 candidates
  (deepseek-coder-v2:16b, deepseek-r1:14b, qwen3:14b, starcoder2:15b,
  ministral-3:14b, gpt-oss:20b) run through the standard bar (fib.py +
  React Counter) with phi4 fixed as architect + reviewer. gpt-oss:20b was
  the only model to pass all 4 runs across both implementer and reviewer
  roles. Added two new presets: `local-oss` (gpt-oss implementer) and
  `local-oss-review` (gpt-oss reviewer + qwen2.5-coder implementer). All
  other candidates auto-`ollama rm`'d. Full report:
  `_bench_2026_04_18/results/RESULTS.md` (gitignored).



- **Multi-Milestone Project Mode** — validated 2026-04-17 with local preset
  (phi4 + qwen2.5-coder:14b). 2/2 milestones approved, $0.0081, 465s.
  Workspace correctly accumulates across milestones. Dashboard page unhidden.
- **Helm chart** — validated 2026-04-17 via `helm lint` (0 failures, 1 cosmetic
  INFO on missing icon) and kubeconform strict schema check (4/4 resources
  valid: Deployment, Service, ConfigMap, Secret). Live cluster install still
  unverified but the chart renders to valid K8s manifests.
- **GitHub PR creation** — wired 2026-04-17 with `--create-pr` /
  `--pr-base` / `--pr-draft` flags + `DELEGATRON_CREATE_PR` env var. PR
  opens on approved runs only; gracefully skips when `gh` CLI is missing
  or no remote is configured. 15 unit tests covering the gate logic,
  happy path, and failure modes. Real end-to-end verified 2026-04-17
  against `borkit/testing` — opened PR #1 with code + pytest tests + the
  full audit-summary body (plan, cost breakdown per role, review verdict,
  acceptance criteria). PR closed + branch deleted after verification.
  Known hygiene issue: `runs/` artifacts land in the PR (filed follow-up
  task to gitignore them). Fixed since: Delegatron's own directories are
  kept out of git through `.git/info/exclude` and never reach a run's
  commits.
- **JavaScript / TypeScript sandbox (React, Next.js, vitest, jest)** —
  validated 2026-04-17 after two infrastructure fixes:
  (1) `DOCKER_IMAGES` auto-selection now picks `node:22-slim` for JS/TS
  workspaces (was silently running `python:3.12-slim`);
  (2) dep-dir caching switched from symlink-through-bind-mount to Docker
  named volumes over `/workspace/node_modules` — iter 2+ now finds
  vitest/jest/etc. reliably. React E2E test: vitest ran, collected 3
  tests across iter 2 & 3. Remaining failures are model-quality (qwen2.5
  struggles with Testing Library idioms), not infrastructure.
- **PHP sandbox** — validated 2026-04-17. Switched image to `composer:2`
  (bundles PHP + composer; `php:*-cli` lacks composer). APPROVED in 2
  iterations, $0.0059, PHPUnit reports 4/4 tests, 4/4 assertions.
- **JavaScript / TypeScript sandbox (React + Vitest + Testing Library)** —
  fully validated with local models 2026-04-17 (second pass). Key finding:
  qwen2.5-coder + phi4 CAN scaffold idiomatic Vitest + Testing Library code
  when (a) the workspace pre-wires `vitest.setup.ts` with
  `@testing-library/jest-dom/vitest`, and (b) the prompt explicitly says
  "use Vitest not Jest, use fireEvent not user-event". With those cues:
  Counter.tsx + Counter.test.tsx generated cleanly, 2/2 tests passed,
  APPROVED. Without them the models mix Jest/Vitest APIs and import
  missing libraries. Guidance added to README.
- **Go sandbox** — validated 2026-04-17. `golang:1.23-bookworm` auto-selected,
  `go mod download` cached in image, `go test` runs under `--network none`.
  APPROVED in 2 iterations, $0.0049.
- **Rust sandbox** — validated 2026-04-17. `rust:1.82-slim-bookworm`
  auto-selected, `cargo build` caches in image, 4/4 tests passed.
  APPROVED in 2 iterations, $0.0054.
- **Java sandbox** — validated 2026-04-17 after two infrastructure fixes:
  (1) Added `java → maven:3.9-eclipse-temurin-21` (and ruby/php) to
  DOCKER_IMAGES + `_LANGUAGE_IMAGE_KEY` mapping.
  (2) Replaced `mvn dependency:resolve` install cmd with
  `mvn dependency:go-offline` followed by a surefire prewarm (dummy
  `DelegatronPrewarmTest.java` to force surefire to resolve
  `surefire-junit-platform` — which go-offline alone misses because
  it's loaded lazily based on detected framework).
  (3) Skipped sanitization of install_cmds (they're factory-generated,
  not agent-generated).
  4/4 JUnit tests passed. APPROVED in 2 iterations, $0.0053.
- **Fan-out parallel tasks** — validated 2026-04-17 after fixing the
  trigger gate. Old condition `len(waves) > 1` only fired for
  multi-stage plans; changed to `any(len(w) > 1 for w in waves)` so a
  single wave of independent tasks (common case) also triggers parallel
  mode. Verified by running 3 independent utility modules in parallel
  git worktrees. 12/12 pytest tests passed. APPROVED in 3 iterations.

## Models evaluated on 12 GB VRAM (benchmarked 2026-04-18)

Standard bar: phi4:14b architect + reviewer, candidate as implementer,
fib.py (Python) + React Counter (TypeScript/Vitest) on RTX 4070 Ti 12 GB.

### Adopted

- [x] **gpt-oss:20b** — 🏆 only model to pass all 4 runs across both
      roles. 13 GB MXFP4, fits 12 GB with modest CPU offload. New
      presets: `local-oss` (implementer) and `local-oss-review`
      (reviewer with qwen2.5-coder impl).

### Rejected (auto-`ollama rm`'d)

- ~~**deepseek-coder-v2:16b**~~ — failed fib.py (dup test file in
  `/workspace` + `/workspace/tests/`), passed React. Not a win.
- ~~**deepseek-r1:14b**~~ — failed both; 128K default context causes
  71% CPU spill on 12 GB GPU. Reasoning-token bloat.
- ~~**qwen3:14b**~~ — failed fib (same dup bug), passed React. No win.
- ~~**starcoder2:15b**~~ — both fail via "patch did not apply"
  (malformed diffs). Fundamentally incompatible with structured-patch
  workflow.
- ~~**ministral-3:14b**~~ — passed fib cleanly, failed React.
  256K default context = 80% CPU spill (54 GB alloc). Not viable.

### Not reachable in Ollama registry

- ~~**Qwen3-Coder 7B**~~ — smallest official tag is 30B.
- ~~**Devstral 2**~~ — only `devstral:24b` (Q4 = 14 GB, overflows
  12 GB; no Q3 shipped).

### Previously dropped

- ~~**GLM-4.6V Flash 9B**~~, ~~**Olmo 3**~~ — not code-tuned.
- ~~**Codestral 22B**~~ — superseded by gpt-oss validation.

### Key findings from the sweep

1. **Default context size is deployment-critical on 12 GB VRAM.**
   Models with 128K+ default context (r1, ministral-3) spill 70–80 %
   to CPU. Weight size alone is misleading.
2. **The fib.py task exposes a real implementer-quality bug** —
   3 of 5 non-gpt-oss models created `test_fib.py` in both the repo
   root AND `tests/`. qwen2.5-coder baseline and gpt-oss do not fall
   for it. May be model-quality OR architect-prompt-specific.
3. **starcoder2 is incompatible** with Delegatron's patch-based
   workflow — generates prose, not diffs. Skip regardless.

## Phase 1: Foundation (MVP)

Core pipeline that can accept a request, plan, code, test, and review in a single loop.

- [x] Provider-agnostic config system (env vars + delegatron.toml + presets)
- [x] Provider adapters: Anthropic, OpenAI, Google, OpenAI-compatible (covers Ollama/Grok/Copilot)
- [x] Pydantic models for all artifact schemas (WorkPlan, PatchSet, TestReport, ReviewReport)
- [x] Architect agent — requirement decomposition and acceptance criteria
- [x] Implementer agent — code generation as structured patches
- [x] Reviewer agent — structured code review with severity ratings
- [x] Git workspace management (branch per run, patch apply, commit, rollback)
- [x] Project detection (language, framework, test/lint commands)
- [x] Docker sandbox runner — apply patches, run tests, collect results
- [x] LangGraph state machine — orchestrate the plan/code/test/review loop
- [x] Basic termination logic — max iterations, pass/fail, budget cap
- [x] CLI entry point (`delegatron run`, `delegatron plan`, `--preset`, `--interactive`)
- [x] Structured logging and event trail
- [x] Schema validation with retry on malformed agent output
- [x] Golden path integration test (FastAPI CRUD app)

## Phase 2: Robustness

Harden the pipeline for real-world use.

- [x] Exponential backoff + jitter for all provider APIs
- [x] Stall detection (repeated failures, no-diff iterations)
- [x] Cost tracking per run with pre-run estimates
- [x] Context management — reset per iteration, fresh injection
- [x] Conflict resolution via evidence hierarchy
- [x] E2B sandbox integration (cloud option; Docker is the default sandbox)
- [x] Async/event-driven execution mode (`AsyncPipeline` is now deprecated; `RunQueue` and the server run `graph.Pipeline`)
- [x] Artifact store (persist WorkPlan, PatchSet, ReviewReport per run)
- [x] Provider call logging with request IDs for tracing
- [x] Checkpoint/resume — save state per iteration, `delegatron resume <run-id>`
- [ ] Prompt caching (Anthropic, OpenAI, Gemini) for cost reduction — the unused interface was removed (#127)
- [x] Human-in-the-loop approval gates at plan and pre-merge stages
- [x] Secrets scanning on generated code
- [x] Dependency auditing on new imports

## Phase 3: Intelligence

Smarter orchestration and richer feedback loops.

- [x] Dual-correction routing (architectural vs implementation feedback)
- [x] Director arbitration for agent disagreements
- [x] Evaluation matrix consensus scoring (EMCS)
- [x] Multi-task fan-out (parallel task execution)
- [x] Git worktree isolation for parallel agents
- [x] Automatic PR creation with audit summary
- [x] Prompt versioning and A/B testing framework
- [x] Quality benchmark mode — test role configurations empirically
- [x] Project-level constraints (no_modify, required_patterns, forbidden_patterns)

## Phase 4: Platform (delegatron.com)

Web interface and team features. Frontend on Cloudflare Pages, API backend on
Cloudflare Workers or dedicated server.

- [x] REST API / webhook interface for triggering runs
- [x] Web dashboard — run history, artifact viewer, cost analytics
- [x] Real-time streaming of agent progress (SSE/WebSocket)
- [x] GitHub App integration (trigger from issues/PRs)
- [x] Team workspaces and API key management
- [x] Run templates and saved configurations
- [x] Plugin system for custom agents and tools
- [x] Frontend tech evaluation: React/Next.js on Cloudflare Pages (static export)
- [x] Authentication (API keys)
- [x] Usage analytics and rate limiting

## Phase 5: Scale

Enterprise and advanced capabilities.

- [x] Queue-based execution with worker pools
- [x] Multi-repo support
- [x] Custom model routing (bring your own models)
- [x] Self-organizing agent topology (dynamic agent addition)
- [x] Benchmark suite and regression testing
- [x] SOC 2 compliance and audit logging
- [x] On-premise deployment option (Docker + Helm)

## Phase 6: Polish & Dashboard Parity

Full feature parity between CLI, API, and web dashboard. Code quality hardening.

- [x] Fix all ruff lint errors
- [x] Harden JSON parsing for local models (Ollama/Qwen/DeepSeek)
- [x] Smart retry logic — distinguish network vs parse errors
- [x] Per-role overrides in API and dashboard (provider, model, base_url, temperature, max_tokens)
- [x] Constraints configuration in API and dashboard (no_modify, required/forbidden patterns)
- [x] Wall-time timeout in API and dashboard
- [x] Fix mypy type errors (add type stubs, configure pydantic plugin)
- [x] Fix pytest configuration (bare `pytest` should work without `python -m`)
- [x] Plan-only mode in API and dashboard (`delegatron plan` equivalent)
- [x] Cost estimator endpoint and dashboard UI (pre-run cost preview)
- [x] Resume UI for interrupted/failed runs
- [x] Template management in API and dashboard (save/list/run/delete)
- [x] Expanded settings page (global pipeline defaults, API key management)
- [x] Benchmark comparison mode in dashboard (run same task across presets)

## Phase 7: Production Readiness & Advanced Features

Real-world hardening, local model optimization, and power-user features.

### Quality & Testing
- [x] End-to-end testing with Ollama presets on real tasks
- [x] Dashboard smoke test (serve + create run flow)
- [x] Integration tests verified against Ollama path
- [x] Friendly error messages for common Ollama failures (not running, model not pulled, OOM)

### Local Model Optimization
- [x] Prompt tuning for Qwen/DeepSeek (structured output hints, shorter schemas)
- [x] Multi-file context — feed existing repo files to implementer
- [x] Model auto-selection based on task complexity

### Power User Features
- [x] Streaming LLM output in CLI (real-time token display)
- [x] Conversation/refinement mode (iterative back-and-forth)

### Documentation
- [x] Comprehensive README rewrite (installation, first run, examples, screenshots)

## Phase 7b: Code Review, Log Analysis & Dashboard UX

### Code Review & Log Analysis
- [x] `delegatron review` CLI command — read-only codebase review, no changes made
- [x] `POST /api/review` endpoint + `/review` dashboard page
- [x] Git URL support — paste GitHub/GitLab/Bitbucket/SSH URL as workspace
- [x] Log file analysis — `--logs` flag for error/pattern/root-cause identification
- [x] Architect suggestions mode — `--with-suggestions` for review + improvement plan
- [x] Review history — `GET /api/reviews` endpoint
- [x] Focus areas — `--focus security|bugs|performance|all`

### Dashboard Features
- [x] Sparkline charts for GPU VRAM, Docker CPU/memory on run detail page
- [x] Active run indicator in sidebar (clickable, auto-updating)
- [x] Planning progress bar with steps and auto-scroll
- [x] Runs page redesign — checkboxes, select all, bulk delete
- [x] Docker image dropdown per run with persistent selection
- [x] Live GPU/Docker stats widget with auto Docker mode switch
- [x] Run comparison page (`/compare`) with side-by-side metrics
- [x] Code viewer with tabbed files on run detail page
- [x] Sandbox info panel (image, OS, shell, timing, pull status)
- [x] Agent reasoning events visible in real-time
- [x] System health widget on home dashboard (Server, Ollama, Docker, Disk)
- [x] Preset performance table in analytics
- [x] Tooltips on Refine/Re-run buttons
- [x] Double-click prevention on action buttons

### Templates & Rulesets
- [x] 28 built-in templates (REST API, auth, database, testing, security, Docker, CI, PowerShell, .NET, Terraform, Kubernetes, etc.)
- [x] OWASP Top 10 and Clean Code constraint rulesets

### Sandbox & Language Support
- [x] 20+ language auto-detection from patch files
- [x] Custom Docker image support per run
- [x] PowerShell auto-detection with Linux PowerShell image
- [x] C# file detection (not just `.csproj`)
- [x] Sandbox overrides Python install for non-Python projects
- [x] Always install pytest for Python projects

### Alerting & Monitoring
- [x] Run failure alerting via `DELEGATRON_ALERT_WEBHOOK_URL`
- [x] GPU memory cleanup after runs (`utils/gpu_cleanup.py`)

## Phase 8: Local-First Architecture

### Local-First / Zero-Cloud
- [x] Native provider registry (direct routing to Anthropic, OpenAI, Google, Ollama adapters)
- [x] Factory refactor (agent_factory, sandbox_factory, security_pipeline)
- [x] Pipeline observer (4 stuck detection patterns, metrics, condensation advisory)
- [x] Observer wired into graph.py pipeline loop
- [x] Zero-cloud verification (NetworkGuard, fingerprinted compliance reports)
- [x] $0 cost tracking for local Ollama models
- [x] Ollama health check in server defaults endpoint
- [x] Removed OpenHands SDK dependency (dead weight for local-first strategy)

## Phase 9: Dashboard UX & Cost Transparency

### Iteration History & Restore
- [x] Iteration History API (list checkpoints, per-iteration diff, restore endpoint)
- [x] IterationTimeline component with visual timeline, diff viewer, restore button
- [x] Per-iteration test/review/patch metadata in API response

### Live Model Reasoning
- [x] Live Thinking panel — real-time token streaming in dashboard
- [x] Extracts readable content from `<think>` blocks and JSON fields
- [x] Server-side streaming enabled (`stream=True`) for all server-triggered runs

### Local GPU Cost Estimation
- [x] nvidia-smi GPU power sampling during agent calls
- [x] Electricity cost calculation (watts * hours * $/kWh)
- [x] Hardware amortization (GPU price / lifetime hours)
- [x] Configurable via DELEGATRON_ELECTRICITY_RATE, GPU_TDP_WATTS, GPU_PRICE_USD, GPU_LIFETIME_HOURS
- [x] Local cost shown in dashboard cost breakdown and CLI estimates
- [x] Cost estimation endpoint includes local GPU projections

### Dashboard Polish
- [x] Tooltips on all interactive dashboard elements (30+ elements)
- [x] AsyncPipeline hard-gated approval (parity with graph.py)
- [x] Review events distinguish reviewer opinion vs gated approval
- [x] Dashboard smoke test fixture fixed (context manager lifecycle)

## Phase 10: Benchmarking, Project Mode & Model Upgrades

### SWE-bench Evaluation
- [x] `delegatron swebench` CLI command and `POST /api/swebench` endpoint
- [x] Run against real GitHub issues from SWE-bench dataset (Lite, Test, Dev splits)
- [x] Per-tier resolution rates (simple, moderate, complex, feature)
- [x] Cost tracking per instance

### Multi-Milestone Project Mode
- [x] `delegatron project` CLI command and `POST /api/projects` endpoint
- [x] Architect decomposes high-level request into milestones
- [x] Each milestone runs through the full pipeline
- [x] Code accumulates across milestones in workspace
- [x] Global cost and time budgets across all milestones
- [x] End-to-end validation — validated 2026-04-17 with local preset:
      2/2 milestones approved, $0.0081, 465s, workspace correctly
      accumulated (fib + factorial modules both present with tests).
      Dashboard sidebar link unhidden.

### Model & Preset Upgrades
- [x] Upgrade Ollama presets from qwen2.5-coder:14b to qwen3-coder:30b
      (later reverted: `local` uses qwen2.5-coder:14b; qwen3-coder:30b is
      `local-heavy`, 24 GB+ VRAM)
- [x] v3 system prompts tuned for local models
- [x] Consolidated local presets: `local`, `local-phi4`, `local-heavy`
      (removed: `local-next` — 80B too slow, `local-qwen25` — folded into `local` as default impl)
- [x] Promoted Phi-4 to default architect + reviewer across all local presets
      (empirically ~4x faster than qwen3.5:9b with better review quality)
- [x] v4 prompts (example-free) — prevents template collapse in 9B models
- [x] `single-claude`, `single-gpt` baseline presets for benchmarking
- [x] Architect plan validation (Apr 16) — all 4 local models produce correct plans

### Server Enhancements
- [x] Run cancel endpoint (`POST /api/runs/{id}/cancel`)
- [x] Run refine endpoint (`POST /api/runs/{id}/refine`)
- [x] Full health check (`GET /api/health/full`)
- [x] System stats endpoint (`GET /api/system/stats`)
- [x] Queue status endpoint (`GET /api/queue`)
- [x] Signed stream URLs for SSE (`GET /api/stream-token`)
- [x] Analytics leaderboard (`GET /api/analytics/leaderboard`)
- [x] Runtime settings endpoints (log-level, log-timezone, parallel-tasks, project-mode)

### Infrastructure
- [x] Repository mapping module (`repomap.py`) for file significance scoring
- [x] Local sandbox (`sandbox/local_runner.py`) for dev/testing without Docker

### Dropped
- OpenHands SDK integration — REMOVED: dead weight, Ollama uses native adapters directly
- Phase 2 (Agent inheritance) — DROPPED: composition over inheritance
- Phase 5 (OH SecurityAnalyzer on patches) — DROPPED: not needed without OpenHands

## Phase 11: Local-Model Convergence & Hardening (PR #129)

See the Unreleased section of `CHANGELOG.md` for details.

### Convergence
- [x] v5 prompts for every local preset; an unset `prompt_version` resolves by endpoint (v1 cloud, v5 local)
- [x] Director agent (`prompts/director/v1.md`) rules once on a recurring conflict (ACCEPT / REVISE / DEFER)
- [x] Per-task stall policy (`no_progress`, `recurring_failure`, `recurring_issues`); a stalled task is skipped, not the run
- [x] `partial` run status and per-task outcomes in `summary.json`
- [x] Pre-run test baseline, flaky-test reruns and regression guard for accepted tests
- [x] Context-window guard for Ollama models (`context_trimmed` events) and search/replace `edits`
- [x] Re-planning only on `fix_target: "architecture"`, capped by `max_replans`
- [x] Plan normalization before execution

### Hardening
- [x] Wall-time limit enforced as a hard deadline; per-role request `timeout_seconds`
- [x] Provider keys sent only to their own API host; server allow-lists for `base_url` and `api_key_env`
- [x] Security findings fed back to the agents; `block` mode retries instead of ending the run
- [x] Literal constraint patterns (`re:` for regexes), `guidelines`, `[security] secrets_allowlist`
- [x] Dirty-workspace guard (`--allow-dirty`) and `.git/info/exclude` for Delegatron's directories
- [x] Server resume continues the run in its own run workspace
