# Changelog

All notable changes to Delegatron will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
adhering to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Public release] — 2026-10-22

Published by StoriaBrevi LLC as a read-only portfolio showcase. No support; issues and pull requests are not accepted.

### Changed
- **License: GNU AGPL-3.0** (was MIT). Copyright (C) 2026 StoriaBrevi LLC.
- **Default Claude model is `claude-sonnet-5-5`.** `claude-sonnet-4-20250514` was retired on 2026-06-15. The Anthropic adapter leaves out `temperature` and forced `tool_choice` for models that reject them.
- **`delegatron serve` binds to `127.0.0.1` by default.** Pass `--host 0.0.0.0` to expose it on the network.
- **The E2B sandbox no longer uploads credential files** (`.env`, `*.pem`, `*.key`, SSH keys); `.env.example` is still uploaded.

- **v5 prompts are the local default.** Every local preset (`local`, `local-phi4`, `local-heavy`, `local-oss`, `local-oss-review`) now pins `prompt_version = 5` for the architect, implementer and reviewer (was 4). The v5 implementer prompt ranks the evidence: failing tests are ground truth, reviewer notes are advice, and only one side of a failing assertion (the code or the test) changes per attempt.
- **An unset `prompt_version` now resolves by endpoint.** A role whose `prompt_version` is unset or 0 (every role of the `mock` preset, and TOML roles that omit it) gets v5 on a local endpoint or the mock provider and v1 on a cloud API. It used to get the newest prompt file, v4, on every endpoint. v4 is unchanged: pin `prompt_version = 4` on a role to keep it.
- **Multi-task runs deliver what they can; new `partial` status.** `max_iterations` is a budget per task. A task that stalls or runs out of iterations is skipped and the run continues with the next task (an exhausted task used to count as completed, and a stall ended the whole run); the tests it left failing are reported to later tasks as pre-existing. A run that approved some but not all of its tasks, or whose approved task's accepted test fails or is gone at the end, ends `partial` and opens no PR; a run a budget limit stopped stays `exhausted` and can be resumed. A stall names its reason (`no_progress`, `recurring_failure`, `recurring_issues`) in the `stall_detected` event and in the task's entry under `task_outcomes` in `summary.json`.
- **Recurring conflicts are arbitrated.** A test failure or set of review issues that recurs on a task goes once to the director (`prompts/director/v1.md`, on the architect's model). Its ACCEPT / REVISE / DEFER ruling updates the review, and REVISE gives the implementer one directed attempt before the stall policy applies. `[pipeline] enable_arbitration = false` turns this off.
- **Server resume continues the run.** `POST /api/runs/{id}/resume` resumes an `error`, `cancelled`, `stalled` or `exhausted` run from its last checkpoint, under the same `run_id` and in the same run workspace, and accepts optional limit overrides. See the API reference.
- **Security findings reach the agents.** Secrets, constraint violations and new dependencies are passed to the implementer and reviewer as review issues. In `security_mode = "block"` a patch with a blocker or major finding is rejected and retried instead of ending the run, and `summary.json` gains a `security` section. `security_mode` is validated (`allow`, `warn` or `block`, any case).
- **Delegatron's own directories are excluded through `.git/info/exclude`.** Run artifacts and scratch directories (`_sandbox/`, `.delegatron-runs/`, `.delegatron-worktrees/`, `.delegatron-venv/`, and `runs/` and `reviews/` when they hold Delegatron's own files) are kept out of git with root-anchored entries, so the repository's `.gitignore` is never edited or committed.
- **The wall-time limit is a hard deadline.** Agent calls, sandbox runs and the pre-run test baseline are cancelled when the time runs out; the run ends `exhausted` with `summary.json`, its final checkpoint and the zero-cloud report written. Elapsed time is checkpointed and continues across `resume`. `summary.json` gains `termination_reason`, `prior_elapsed_seconds` and `session_elapsed_seconds`, and the `terminated` event names the limit, phase and iteration.
- **Model requests time out.** A role's `timeout_seconds` bounds each provider request: 600 s by default (the OpenAI and Anthropic SDKs' own default), 900 s on a local endpoint; when streaming, it applies to the gap between chunks. It must be a positive number of seconds (anything else falls back to the default).
- **`max_cost_usd = 0` in `delegatron.toml` or `DELEGATRON_MAX_COST_USD=0` means no cost limit**, as `--max-cost 0` already did; it used to be raised to $0.01.
- **Pre-run test baseline.** Before the first patch the workspace's tests run once (`[pipeline] baseline_tests`, `DELEGATRON_BASELINE_TESTS`); what already fails is reported as pre-existing and never blamed on the run. `summary.json` gains `fixed_baseline_failures`, `regressed_tests` and `flaky_tests`; a run whose approved task's test fails again ends `partial`.
- **Flaky tests.** Failing tests are rerun up to `[pipeline] flaky_reruns` times (default 2, at most 5; `flaky_rerun = false` turns reruns off); `tolerate_flaky_tests` (also accepted as `tolerate_preexisting_flaky`) passes a run whose only failures are pre-existing tests that passed on a rerun, never one in a file the run wrote.
- **Search/replace edits and deletions.** A patch may change a file through `edits` (`path`, `search`, `replace`, `count`) instead of rewriting it; an excerpted file can only be changed that way. An edit that does not match is reported to the implementer, and the other files still apply. A patch may also remove files through `delete_files`; `summary.json` lists them under `files_deleted`.
- **Context-window guard.** Prompts that would overflow a local model's window are trimmed: the repository map first, then related files, and only then are the files the attempt must edit shortened to head+tail excerpts. Every trim is a `context_trimmed` event (role, keys, token counts, dropped and excerpted files); a dropped file is listed under "Files Not Shown". Only a server that answers like Ollama is sized; others have an unknown window unless `context_window` is set.
- **Recurring failures are recognised by shape.** Numbers, quoted literals, hex addresses and durations are masked in failure signatures, so `12 != 10` then `14 != 12` is one recurring failure.
- **Re-planning.** The architect re-plans only for a blocker or major review issue with `fix_target: "architecture"`, or a director ruling that the plan itself must change, on any preset; words such as "design" or "approach" in an issue no longer trigger it. Re-plans are capped by `[pipeline] max_replans` (default 2); later requests go to the implementer (`replan_suppressed`). Each re-plan is saved as `work_plan_r<n>.json`, and the architect sees the completed tasks and the task in progress.
- **Locality covers the LAN.** Private, link-local and CGNAT addresses, `.localhost` / `.local` / `.lan` / `.internal` / `.home.arpa` names and single-label names (a Compose service such as `ollama`) count as local for cost, prompt versions, timeouts and the zero-cloud report. `[pipeline] local_hosts` or `DELEGATRON_LOCAL_HOSTS` adds host names (e.g. a LAN model server's FQDN) the zero-cloud report counts as local. A priced cloud model behind a local proxy (LiteLLM) is billed at its API price; prompt-cache read and write tokens are priced at the cache rates.
- **Renamed agent input headings**: implementer "Framework Hints", "Pre-existing Failures", "Flaky Tests"; reviewer "Overall Acceptance Criteria (whole plan)"; architect "Completed Tasks" and "Current Task". A skipped test run shows as `Result: SKIPPED`, and Go test failures are named by their test ids. Retry notes come from `prompts/truncation_retry/v1.md` and `prompts/validation_retry/v1.md`, and the project planner's prompt from `prompts/meta_architect/v1.md`.
- **Run branches.** A run branch forks from the requested base branch (`run --branch`), else the checked-out branch; a fresh run whose branch exists gets `delegatron/run-<id>-<n>`. `run` and `review` take `--branch`, the branch to clone when `--workspace` is a git URL. Run commits skip the repository's git hooks unless `[pipeline] run_git_hooks = true`.
- **Dirty workspaces are refused.** A run does not start when the git workspace has uncommitted changes outside Delegatron's own directories (they would be mixed into the run's commits). `--allow-dirty` on `run`, `resume`, `retry`, `refine`, `benchmark`, `project` and `template run` starts it anyway.
- **Results and PRs.** `summary.json` also gains `task_outcomes`, `skipped_tasks`, `files_changed`, `files_deleted`, `baseline_failures`, `branch`, `workspace_root`, `roles`, `prior_cost_usd` and `session_cost_usd`; cost and elapsed time add up across resumed sessions. `delegatron show` prints the stop reason, each task's outcome, regressed tests, fixed pre-existing failures and deleted files; the PR body lists regressed and pre-existing failures. The server alerts on `partial` runs too, and `/api/review` sizes its input to the reviewer's context window like `delegatron review`.
- **Config file and role settings.** The CLI reads `<workspace>/delegatron.toml`, then `./delegatron.toml`, unless `--config` names a file. With `--preset`, a `[roles.<role>]` table is merged over the preset's role key by key (it used to replace the whole role); changing `provider` or `base_url` drops the preset's `base_url`, keys and `prompt_version`. A role's `temperature` is now honored (the configured value used to be ignored and 0.0 sent): clamped to 0–2, at most 1.0 for Anthropic models. Implementer retries that would repeat the previous attempt use `[pipeline] retry_temperature` (default 0.4) when it is higher. New role key `context_window` (see the context-window guard).
- **Constraints match literal text.** `required_patterns` and `forbidden_patterns` entries are literal text unless prefixed with `re:` (they were all regular expressions: prefix existing regex entries with `re:`); an invalid one fails when the config loads. `forbidden_patterns` are checked only on the lines a patch adds, and each `required_patterns` entry must appear in at least one changed source file of a patch (it was every changed file). `no_modify` matches gitignore-style. The `[constraints]` table of `delegatron.toml` is now applied (it was never read; only API and template constraints were), as is `[pipeline.constraints]`, which wins per key. New: `guidelines` (prose rules shown to the agents, never pattern-checked) and `[security] secrets_allowlist` (regexes for lines the secrets scanner must not report).
- **Sandboxes.** Test reports carry per-test results, failure excerpts and a skipped reason, and the implementer is shown the failing tests and the files they name. Dependency manifests a patch adds are installed on the next test run. In the Docker and E2B sandboxes the task's Python scripts are smoke-run after the tests and their exit code and output are reported with the test results (`[pipeline] run_entrypoints`; off by default in the local sandbox).
- **Plans are normalized** before they run: duplicate or empty task ids, `depends_on` entries naming no task, unsafe output paths and code tasks without a test file are repaired; a plan with no tasks ends the run with `error`. The reviewer sees the attempt's diff, the changed files and related unchanged files, and the tasks that are out of scope.
- **Server run workspaces.** When the server's workspace is the root of a git repository, each run's workspace (`<workspace>/.delegatron-runs/<run_id>`) is a clone of it (same history and `origin`, so `create_pr` can open a PR), otherwise a fresh repository. The file view and downloads return every file the run created or modified across all iterations and tasks; `POST /api/runs/{id}/restore` resets the run workspace to an iteration's commit; deleting a run removes its workspace. `POST /api/runs` and `/api/review` accept `branch` for a git-URL `workspace`. Served directly with uvicorn (as the Docker image is), the server takes its workspace from `DELEGATRON_WORKSPACE`.
- **Docker image and Compose.** The image installs `git` and the `cloud-providers` extra (build arg `EXTRAS`, from `DELEGATRON_EXTRAS` in Compose), and sets `DELEGATRON_WORKSPACE`, `DELEGATRON_ARTIFACTS_DIR` and `DELEGATRON_DATA_DIR` to `/app/workspace`, `/app/runs` and `/app/data`. Compose mounts `./data`, defaults `DELEGATRON_MAX_WALL_TIME` to 60 and no longer starts an unused Redis service. The image has no Docker CLI: use the E2B sandbox or the Docker-socket setup in `docs/deployment.md`.
- **Dashboard.** Shows `partial` runs. Settings stores only the defaults you change, so the server's defaults (`/api/defaults`) apply to the rest.
- The `cloud-providers` extra requires `google-genai>=1.22.0`.

### Deprecated
- `orchestrator/async_runner.AsyncPipeline` warns when constructed. It is an outdated copy of the pipeline loop (first task only, `diff_unified` patches only), not a wrapper around it. `RunQueue` now runs `graph.Pipeline`; use `graph.Pipeline` with a `step_callback` instead (see the Python SDK section of the API reference).

### Security
- A provider's default API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) is sent only to that provider's own API host. `openai-compatible` roles have no default key. A hosted OpenAI-compatible API (Groq, OpenRouter, xAI), or a `base_url` on another host (Azure, a LiteLLM proxy), needs `api_key_env` naming its key. Ollama needs no key.
- Server role overrides can no longer send the server's keys to a caller-chosen `base_url` or read arbitrary environment variables. A `base_url` that is not trusted gets no server key, and `api_key_env` is limited to the provider's default, the variable the operator configured and the names in `DELEGATRON_ALLOWED_KEY_ENVS`. `DELEGATRON_ALLOWED_BASE_URLS` lists extra endpoints that keep the server's keys.
- `POST /api/runs` refuses a `config_path` inside a Delegatron directory (a run workspace or the run artifacts), so a TOML file an earlier run wrote cannot be loaded as operator config.
- Security findings are judged against a per-run baseline: a secret, forbidden pattern or protected-file change stays reported until it is actually removed, and in `block` mode a patch with blocking findings is never written or committed.
- `.git` in any spelling (case, trailing dots, `git~1`), NTFS alternate data streams and reserved Windows device names are rejected as patch paths.
- A git-URL workspace must be `https://`, `ssh://` or `user@host:path` (a bare `github.com/...` is cloned over https); option-like values, `ext::`/`file:` transports and plain `http://` URLs are rejected before git runs. The server also limits the host to `DELEGATRON_GIT_ALLOWED_HOSTS` (default `github.com,gitlab.com,bitbucket.org`).

### Removed
- Billing and Stripe stubs, white-label branding, and the parked OpenClaw / NemoClaw integration.
- The developer's personal test-VM and installer scripts (`scripts/`).
- `utils/prompt_cache.py`: it was never called by any provider, so Delegatron has never done prompt caching (the 0.1.0 entry below was wrong), and its Anthropic and Gemini message shapes are rejected by those APIs

### 2026-04-17 session (summary)

**Infrastructure bugs fixed (7 silent breakages):**
- Ollama restart endpoint (missing `httpx` import — had never worked)
- Non-git workspace false-failure gate (runs rejected even when tests passed)
- `DOCKER_IMAGES` dead code — now wired so JS/Go/Rust/Java/etc. projects get the right Docker image instead of silently running `python:3.12-slim`
- `node_modules` cache broken between iterations (symlink-through-bind-mount) — switched to Docker named volumes
- Maven install missed `surefire-junit-platform` — added `dependency:go-offline` + surefire prewarm
- Shell-command sanitizer blocked factory-generated install commands
- Fan-out parallel gate was too strict (missed single-wave parallel case)
- Template rendering was a silent no-op for `${var}` syntax (all 30 built-ins affected)

**E2E validations added:**
- Multi-milestone Project Mode (2/2 milestones approved, $0.0081)
- Go / Rust / Java / PHP sandbox E2E runs (all approved with tests passing)
- Fan-out parallel tasks (3 independent modules in concurrent worktrees)
- Real GitHub PR creation against a private test repository (4 PRs, all verified then closed)
- React + Vitest + Testing Library (local models, with framework hints)

**Features wired from dead code:**
- `--create-pr` / `--pr-base` / `--pr-draft` CLI flags + `DELEGATRON_CREATE_PR` env var
- Same three fields now accepted by `POST /api/runs` so the dashboard can request PRs

**New guardrails:**
- `framework_hints.py`: per-project implementer guidance (Vitest vs Jest, available deps) injected into the user message — fixes 14B-model API mixing
- `security/import_validator.py`: multi-language import checker (JS, Python, Go, Rust, Java) — unresolvable imports get surfaced as review feedback for the next iteration
- Auto-gitignore for `runs/`, `_sandbox/`, `.delegatron-worktrees/` with collision safety — user repos no longer accumulate run artifacts
- Template `run` CLI now accepts `--preset` and `--create-pr` (was hardcoded to template's baked preset, which no built-in template sets)

**Dashboard polish:**
- `/templates` page: search box, Built-in / Mine / All tabs, variable listings, delete button hidden for built-ins
- 30 built-in templates now auto-load (shipped in previous commit, now exercised via UI)

**Test + parser fixes:**
- Sandbox test-output parser handles Go `-v`, Rust double-result, Maven surefire, PHPUnit, Vitest (was pytest-only, silently showed "0 tests" for everything else)
- 151 new parametrized tests validating every shipped template renders cleanly

**Test count:** 1049 → **1421+** unit tests pass, ruff + format clean.

**Preset guidance update:** `local-heavy` (qwen3-coder:30b implementer) is now explicitly flagged as **requires 24 GB+ VRAM**. On smaller cards it spills 30+ GB to system RAM, runs 3× slower per token, and exhausts the iteration budget on any non-trivial task. Keep `local` (qwen2.5-coder:14b) or `local-phi4` (phi4 all roles) for 12 GB cards — both fit fully in VRAM.

### Added
- **SWE-bench evaluation**: `delegatron swebench` CLI command and `POST /api/swebench` endpoint — evaluate pipeline against real GitHub issues from the SWE-bench dataset with per-tier resolution rates
- **Multi-milestone project mode** (validated 2026-04-17): `delegatron project` CLI command and `POST /api/projects` endpoint — architect decomposes high-level requests into milestones, pipeline accumulates code across them. Validated with local preset, 2/2 milestones approved.
- **Consolidated local presets**: `local` (phi4:14b + qwen2.5-coder:14b + phi4:14b, fits 12 GB), `local-phi4` (phi4:14b in all roles — fastest, fits 12 GB), `local-heavy` (phi4 + qwen3-coder:30b + phi4, **requires 24 GB+ VRAM**). Removed `ollama-*` legacy presets, `local-next` (80B too slow), and `local-qwen25` (folded into `local` as default implementer)
- **Phi-4 as default local architect + reviewer**: empirically ~4× faster than qwen3.5:9b with better review quality (79s vs 312s on code review). All local presets now use phi4:14b for architect/reviewer roles
- **v4 system prompts**: example-free prompts for local models that prevent template collapse in 9-14B models; v4 architect prompt rewritten to eliminate hallucinations (stale Ollama KV-cache could previously leak chess-engine boilerplate into unrelated plans)
- **Baseline presets**: `single-claude`, `single-gpt` for benchmarking
- **Repository mapping**: `repomap.py` module generates significance-scored file maps for agent context
- **Local sandbox**: `sandbox/local_runner.py` for development/testing without Docker isolation
- **Run cancel endpoint**: `POST /api/runs/{id}/cancel` to stop running pipelines
- **Run refine endpoint**: `POST /api/runs/{id}/refine` to re-run focusing on unresolved review issues
- **Full health check**: `GET /api/health/full` with Ollama, Docker, disk, and provider status
- **System stats endpoint**: `GET /api/system/stats` for live GPU, Docker, and pipeline statistics
- **Queue status endpoint**: `GET /api/queue` for run queue monitoring
- **Signed stream URLs**: `GET /api/stream-token` returns HMAC-SHA256 signed URLs for SSE (prevents API key leakage)
- **Analytics leaderboard**: `GET /api/analytics/leaderboard` for preset performance comparison
- **Runtime settings endpoints**: `PUT /api/settings/log-level`, `log-timezone`, `parallel-tasks`, `project-mode`
- **Iteration History UI**: browse per-iteration checkpoints on run detail page — visual timeline with test/review status, expandable diff viewer, and "Restore to #N" button
- **Iteration History API**: `GET /api/runs/{id}/iterations` (list checkpoints), `GET /api/runs/{id}/iterations/{n}/diff` (per-iteration diff), `POST /api/runs/{id}/restore` (restore to checkpoint)
- **Live Thinking panel**: real-time model reasoning stream during active runs — extracts readable content from `<think>` blocks (local models) and JSON fields, auto-collapses when stage completes
- **Local GPU cost estimation**: nvidia-smi power sampling, electricity + hardware amortization for local models instead of $0.00 — shows real costs in dashboard and CLI
- **GPU cost in estimates**: `delegatron estimate` and `POST /api/estimate` now include local GPU cost projections for Ollama presets
- **Tooltips on all dashboard elements**: StatusBadge, action buttons (Cancel, Resume, Download, Compliance, Approve, Reject), IterationTimeline, RunCard, Sidebar, Templates, Review page, Runs page
- **Environment variables**: `DELEGATRON_ELECTRICITY_RATE`, `DELEGATRON_GPU_TDP_WATTS`, `DELEGATRON_GPU_PRICE_USD`, `DELEGATRON_GPU_LIFETIME_HOURS` for local GPU cost configuration

### Changed
- **License**: switched to MIT; added `THIRD_PARTY_LICENSES.md` with attributions for all direct dependencies
- **Benchmark page removed** from dashboard — out of scope for the bootstrap-generation niche; `delegatron benchmark` CLI remains
- **Projects page hidden** from dashboard sidebar until Multi-Milestone Project Mode is validated end-to-end
- **Dashboard port**: `.env.production` now targets `:8001` (Windows `svchost` reserves `:8000` on some systems, blocking our server from binding)

### Fixed
- **Ollama restart endpoint**: `POST /api/ollama/restart` was missing its `httpx` import and failed on first call for every user. Verified end-to-end: 9.6s roundtrip, all models restored after restart
- **AsyncPipeline approval bug**: async_runner.py now mirrors graph.py hard-gated approval — never trusts raw `acceptance_met` from reviewer; tracks `patch_applied` flag through state
- **Review event accuracy**: `review_saved`, `reviewer_reasoning`, `reviewer_complete` events now emit both `reviewer_accepted` (raw opinion) and `accepted` (gated approval) to prevent misleading audit logs
- **Dashboard smoke test**: fixture now uses `with TestClient(app) as client: yield client` context manager so background tasks are properly cleaned up
- **Server streaming**: pipeline.execute() now called with `stream=True` in the server path so token events reach the dashboard Live Thinking panel

### Docs
- Synced README, user-guide, and CLAUDE.md to current `local` preset models (phi4:14b + qwen2.5-coder:14b) and added the missing `local-phi4` / `local-heavy` entries
- Added 10 missing environment variables to user-guide.md (DATA_DIR, AUTH_ENABLED, ALERT_WEBHOOK_URL, GPU_* set, MODEL overrides, DOCKER_IMAGE, WEBHOOK_SECRET)
- Flagged OpenClaw integration as PARKED in user-guide.md to match openclaw.md
- Added model validation scripts (`benchmarks/test_gpt_oss_architect.py`, `benchmarks/test_architect_plans.py`) and interim benchmark docs under `docs/`
- **Reorganized `scripts/`**: moved 8 research/benchmark scripts (`benchmark-local-models.py`, `benchmark_swebench_architects.py`, `full_pipeline_validation.py`, `run_model_benchmark.py`, `run_swebench_test.py`, `test_architect_plans.py`, `test_gpt_oss_20b.py`, `test_gpt_oss_architect.py`) into a new top-level `benchmarks/` directory. `scripts/` now holds only deployment + bootstrap code, matching its stated scope.
- Removed dated one-off test logs (`docs/test-results-2026-04-15.md`, `test-results-2026-04-16.md`) and the 3 seed research PDFs/docx under `docs/research/` — superseded by the implementation and by `docs/capabilities.md`

## [0.3.0] - 2026-04-09

### Added
- **Read-only code review**: `delegatron review` CLI command, `POST /api/review` endpoint, `/review` dashboard page — audits a codebase without making changes
- **Log file analysis**: `delegatron review --logs app.log` or paste logs in dashboard — identifies errors, root causes, patterns, and fixes
- **Architect suggestions mode**: `--with-suggestions` flag runs reviewer + architect together to produce issues + improvement plan
- **Git URL support**: paste a GitHub/GitLab/Bitbucket/SSH URL anywhere a workspace path is accepted — Delegatron clones automatically
- **Custom Docker image**: `docker_image` config field supports Windows containers, Node, Go, Rust, .NET, or any image
- **PowerShell auto-detection**: Windows containers use `powershell -NoProfile -Command` when commands reference `.ps1`
- **Sandbox info panel**: run detail page shows image, OS, shell, timing, pull status, network isolation
- **Agent reasoning events**: see architect/implementer/reviewer thinking in real-time in the event log
- **Progress bar**: real-time progress with phase label, token count, cost, and ETA
- **System health widget**: home dashboard shows Server, Ollama, Docker, and Disk status in one row
- **Preset performance table**: analytics page shows approval rate, error rate, avg cost per preset
- **Run failure alerting**: `DELEGATRON_ALERT_WEBHOOK_URL` sends POST on error/cancel/exhausted
- **Review history**: `GET /api/reviews` lists past code reviews
- **28 built-in templates**: REST API, auth, database, testing, security, Docker, CI, refactoring, docs, bug fix, Windows service, PowerShell module/automation, WPF app, .NET API, Terraform (AWS/Azure/GCP), Kubernetes, migration, monitoring, WebSocket, CLI tool, data pipeline, microservice, React component, Arduino
- **Rulesets**: OWASP Top 10 and Clean Code constraint templates
- **Code viewer**: tabbed file viewer on run detail page
- **Run comparison**: `/compare` page with side-by-side metrics
- **Toast notifications**: success/error popups on actions
- **Dark/light mode toggle**
- **Landing page**: `site/index.html` for delegatron.com

### Dashboard
- **Sparkline charts**: GPU VRAM and Docker CPU/memory stats on run detail page
- **Active run indicator**: clickable, auto-updating badge in sidebar
- **Planning progress bar**: step-by-step progress with auto-scroll to plan result
- **Runs page redesign**: checkboxes, select all, bulk delete
- **Docker image dropdown**: select custom Docker images per run, persists selection
- **Live GPU/Docker stats widget**: auto Docker mode switch for all languages
- **Tooltips**: Refine/Re-run buttons explain the difference
- **Double-click prevention**: on Refine/Re-run/Resume buttons
- **Download filename**: generated README renamed to `DELEGATRON_RUN.md` in zip

### Changed
- Dashboard UX polish: responsive grids, accessibility (aria-current, keyboard nav), empty states, centralized getApiUrl()
- Setup scripts: Ollama-first messaging on both setup.sh and setup.bat
- Workspace input fields updated to accept git URLs with hints
- Sandbox detects language from patch files (not workspace) for accurate tool selection
- Docker images corrected for PowerShell and React templates
- `.cs` file detection for C# projects (not just `.csproj`)
- Sandbox overrides Python install commands when non-Python project detected
- Always install pytest for Python projects in sandbox

### Fixed
- Docker image dropdown not persisting selection
- `docker_image` was never applied to pipeline config in normal runs
- Execute Plan now skips interactive mode when user already reviewed plan
- Settings persistence for log level, timezone, and Save All button
- New Run uses Settings values (localStorage overrides server defaults)
- Auth-enabled dashboard, active run lookup, package data fixes
- Reviews stored in separate `reviews/` directory (not install dir)
- Dashboard crash when review artifacts missing `run_id`
- `.env.production` added so dashboard finds API on port 8000

## [0.2.0] - 2026-04-09

### Added
- **Zero-cloud verification**: `NetworkGuard` records all network interactions during pipeline runs and produces a SHA-256 fingerprinted `ZeroCloudReport` proving whether data stayed on local infrastructure
- **Compliance dashboard** (`/compliance`): visual verification page with green/red verdict, event log, provider breakdown, fingerprint display, and JSON export for auditors
- **Compliance API endpoint** (`GET /api/runs/{id}/compliance`): export zero-cloud report programmatically
- **Code viewer** on run detail page: tabbed file viewer with language hints, loads generated code from new `GET /api/runs/{id}/files` endpoint
- **Run comparison page** (`/compare`): side-by-side metrics table for any two runs with color-coded diffs
- **Ollama status indicator** in sidebar: live health check showing running status and model count
- **Dark/light mode toggle**: persists to localStorage, sun/moon button in sidebar
- **Toast notification system**: success/error/info popups on approve, reject, rerun, resume actions
- **Compliance badge on home dashboard**: Zero-Cloud card linking to compliance page
- **Expandable event rows** in run detail: click any event to see all fields, keyboard accessible
- **Ollama health check** in `/api/defaults`: probes Ollama and returns running status + available models
- **$0 cost tracking** for local models: Ollama models (qwen, deepseek, llama, mistral, etc.) correctly tracked as free
- **OpenClaw security module**: egress policy, audit middleware, and secret masking ported from NemoClaw concepts into pure Python

### Changed
- **Removed OpenHands SDK dependency** (-1,383 lines): provider routing is now direct to native adapters (Anthropic, OpenAI, Google, OpenAI-compatible) with no LiteLLM middle layer
- **Python 3.11+ supported** (was 3.12+ due to OpenHands requirement)
- **Sandbox options simplified** to `docker` and `e2b` (removed `openhands-docker`)
- **Registry rewritten** (108 lines → 65 lines): simpler, direct provider routing
- **Observer renamed** from `oh_state.py` to `observer.py` (Delegatron's own code, not OpenHands)
- **Setup script rewritten** as Ollama-first: detects Ollama, checks pulled models, guides local setup before cloud
- **Cloud key warning** downgraded from "ALL RUNS WILL FAIL" to informative "local presets work without API keys"
- **Optional dependency group** renamed from `[legacy-providers]` to `[cloud-providers]`
- **Anthropic and Google providers** un-deprecated (they're the primary adapters now, not fallbacks)
- **Google provider** `_HAS_GOOGLE` NameError fixed (was crashing on import when SDK not installed)

### Fixed
- Egress policy wildcard matching: `re.match` → `re.fullmatch` to prevent subdomain bypass
- Download endpoint filename sanitization: stripped all special characters to prevent header injection
- Mock provider classified as non-local in zero-cloud reports
- Mock models charged $0.021 instead of $0.00
- Benchmark test expected cost > 0 for mock (now correctly expects >= 0)
- EventSource disconnect now shows event in UI instead of silently dying
- Plan approve/reject handlers validate response status and show errors
- Compliance page side-effect in render moved to useEffect
- Pre-existing TypeScript errors in new run and run detail pages
- Suspense boundary added for useSearchParams in compliance page

### Removed
- `compat/` directory (oh_llm.py, oh_provider.py) — OpenHands LLM bridge
- `sandbox/oh_sandbox.py` — experimental sandbox with no network isolation
- `test_compat.py`, `test_oh_sandbox.py` — tests for removed code
- 8 trivial tests (constructor/enum checks that tested nothing real)
- `openhands-sdk`, `openhands-workspace`, `openhands-tools` from dependencies

### Dashboard UX
- Responsive grids across all pages (compliance, benchmark, settings, new run)
- Accessibility: aria-current, aria-expanded, role attributes, keyboard navigation on event rows
- Empty states with action buttons on home, runs, analytics pages
- Centralized `getApiUrl()` (was duplicated in 3 files)
- Loading states with context ("Loading runs..." instead of generic "Loading...")

## [0.1.1] - 2026-04-05

### Added
- Atomic billing reservation with `BEGIN IMMEDIATE` transaction
- Two-phase billing flow (reserve at creation, record at completion)
- Bounded subscriber queues in AsyncPipeline (maxsize=256)
- SSE event drop logging for slow consumers
- SQLite persistence for auth, teams, and billing
- Deployment guide with Docker Compose and Kubernetes examples
- Windows setup.bat for local installation
- Dashboard migrated to Tailwind CSS v4

### Fixed
- Billing quota race condition on concurrent requests
- Pipeline bootstrap failures now produce error summaries
- Cancelled runs save summary.json
- EMCS score captured in AsyncPipeline (parity with sync Pipeline)
- Billing slot leak on zero-cost errors

## [0.1.0] - 2026-04-01

### Added
- Initial project scaffolding and repository structure
- Provider-agnostic agent system (Anthropic, OpenAI, Google, OpenAI-compatible)
- LangGraph-style orchestration loop (plan/code/test/review)
- Docker and E2B sandbox isolation
- CLI with run, plan, resume, show, estimate, benchmark, and serve commands
- FastAPI server with REST API and SSE streaming
- Multi-tenant auth with API keys and team management
- Artifact persistence (summary.json, events.jsonl, state checkpoints)
- ~~Prompt caching for Anthropic, OpenAI, and Google providers~~ (never wired in; removed, see Unreleased)
- EMCS scoring and arbitration for reviewer conflicts
- Constraint validation and secrets scanning
- Rate limiting and audit logging middleware
- Plugin system with hook manager
- Mock provider for testing without API keys
