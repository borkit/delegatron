# CLAUDE.md — Delegatron Development Guide

## Project Overview

Delegatron is a multi-agent code bootstrap pipeline built by StoriaBrevi LLC.
It orchestrates LLMs (any provider) through a state-machine loop to produce tested,
reviewed code from natural language requests. Roles are provider-agnostic — any model
can fill any role (architect, implementer, reviewer).

## Tech Stack

- **Language:** Python 3.11+
- **Orchestration:** Async state-machine loop in `orchestrator/graph.py` (LangGraph-style plan/code/test/review cycle; LangGraph itself is not a dependency)
- **LLM Providers:** Ollama (local), OpenAI, Anthropic, Google AI, and any OpenAI-compatible endpoint (Anthropic and Google SDKs come with the `cloud-providers` extra)
- **Sandbox:** Docker (primary), E2B microVMs (cloud option), local (no isolation)
- **Package Manager:** pip with pyproject.toml
- **Testing:** pytest
- **Linting:** ruff
- **Type Checking:** mypy (strict)
- **Formatting:** ruff format

## Repository Layout

```
src/delegatron/
    __init__.py             # Package root, version
    analytics.py            # Usage analytics — tracks pipeline run metrics
    artifacts.py            # Artifact store — persists run artifacts to disk
    audit.py                # SOC 2 audit logging — immutable event log for compliance
    autoselect.py           # Automatic preset selection based on task complexity
    benchmark.py            # Quality benchmark — compare pipeline configs empirically
    benchmark_suite.py      # Regression benchmark suite for validating quality over time
    cli.py                  # CLI entry point (click)
    config.py               # Provider-agnostic settings, presets, TOML loading
    constraints.py          # Project-level constraints — enforce rules on generated code
    context.py              # Context management — selects relevant files for implementer
    detection.py            # Project language/framework auto-detection
    framework_hints.py      # Framework-specific hints for the implementer (Vitest vs Jest, ...)
    multirepo.py            # Multi-repo support — clone, sync, track multiple repos
    project.py              # Multi-milestone project orchestration
    repo_resolve.py         # Resolve workspace args — local path or git URL
    repomap.py              # Repository structure mapping and file significance scoring
    plugins.py              # Plugin system for custom agents, providers, sandboxes, hooks
    prompts.py              # Prompt loader — loads versioned system prompts from files
    queue.py                # Queue-based execution with async worker pools
    ratelimit.py            # Sliding-window in-memory rate limiter
    routing.py              # Custom model routing (cost, latency, capability strategies)
    swebench.py             # SWE-bench evaluation — run against real GitHub issues
    templates.py            # Run templates for reusable pipeline configurations
    topology.py             # Self-organizing agent topology with auto-scaling
    workspace.py            # Git workspace management (branch, patch, rollback)
    worktree.py             # Git worktree isolation for parallel task execution
    agents/
        __init__.py
        base.py             # Abstract agent interface
        architect.py        # Architect agent (default: Claude)
        implementer.py      # Implementer agent (default: GPT)
        reviewer.py         # Reviewer agent (default: Gemini)
        director.py         # Director agent — binding arbitration of recurring conflicts
        rendering.py        # Shared markdown building blocks for agent user messages
        providers/
            __init__.py
            base.py         # Abstract base class for LLM provider adapters
            anthropic.py    # Anthropic Claude provider adapter (requires anthropic SDK)
            openai_provider.py  # OpenAI / OpenAI-compatible provider adapter (Ollama)
            google.py       # Google Gemini provider adapter (requires google-genai SDK)
            mock.py         # Mock provider for testing without API keys
            registry.py     # Provider registry — direct routing to native adapters
    factories/
        __init__.py
        agent_factory.py    # Centralized agent creation from Config
        sandbox_factory.py  # Centralized sandbox creation (docker/e2b/local)
        security_pipeline.py # Centralized security checks (constraints/secrets/deps)
    orchestrator/
        __init__.py
        arbitration.py      # Director arbitration — detects recurring conflicts, applies the director's rulings
        async_runner.py     # Deprecated standalone runner (queue and server use graph.Pipeline)
        evidence.py         # Reviewer diff, flaky tests from history, entrypoints to smoke-run
        fanout.py           # Execution waves — which tasks are ready to run together
        graph.py            # Pipeline orchestration loop (plan/code/test/review)
        observer.py         # Pipeline observer (stuck detection, metrics)
        plan_normalize.py   # Normalize architect plans (task ids, dependencies, test outputs)
        router.py           # Conditional routing logic
        scoring.py          # EMCS quality scoring for iteration decisions
        state.py            # AgentState TypedDict, reducers
        suite_guard.py      # Test-suite guards — accepted tests, known failures, test paths
        termination.py      # Termination criteria, stall detection
    sandbox/
        __init__.py
        base.py             # Abstract sandbox interface
        counts.py           # Test counts (total, failures) from runner output, for every sandbox
        docker_runner.py    # Docker sandbox (default)
        e2b_runner.py       # E2B sandbox integration
        local_runner.py     # Local sandbox (no isolation, for dev/testing)
        parse_check.py      # Parse/lint gate — syntax errors fail fast, advisories reported
        prepare.py          # Install/test command derivation, targeted tests, step failures
        toolchains.py       # Language toolchain rules and Docker image selection
    schemas/
        __init__.py
        work_plan.py        # WorkPlan model
        patch_set.py        # PatchSet model
        test_report.py      # TestReport model
        review_report.py    # ReviewReport model
        arbitration.py      # ArbitrationReport model (director rulings)
        events.py           # Event envelope model
    security/
        __init__.py
        secrets.py          # Secrets scanner — detect leaked credentials in patches
        deps.py             # Dependency auditing — flag new third-party imports
        import_validator.py # Unresolvable imports in a patch (advisory)
        patch_lines.py      # Lines a patch adds, per file (what the scanners check)
        zero_cloud.py       # Zero-cloud report — audits whether data left the local network (blocks nothing)
    integrations/
        __init__.py
        git_pr.py           # Git PR creation — push branch and open pull requests
    server/
        __init__.py
        app.py              # FastAPI server — REST API + SSE streaming (35+ endpoints)
        auth.py             # Authentication — API key management and user identity
        db.py               # Shared SQLite database for server stores
        run_workspace.py    # Run workspaces (.delegatron-runs/<run_id>): clone or copy, lookup, cleanup, run files
        teams.py            # Team workspace management
    utils/
        __init__.py
        retry.py            # Exponential backoff with jitter
        context_budget.py   # Token estimates; trim agent input to the context window
        cost.py             # Token/cost tracking + local GPU cost estimation
        gpu_cleanup.py      # GPU memory cleanup for Ollama models after runs
        logging.py          # Structured logging setup
        paths.py            # Safe workspace-relative paths, Delegatron's internal dirs
        provider.py         # Vendor-neutral API normalization, local-endpoint check
        test_output.py      # Parse test runner output (per-test outcomes, failure excerpts)
prompts/                    # Versioned system prompts (external, not hardcoded)
    architect/v1.md         # Cloud models (Claude, GPT, Gemini); auto for cloud roles (prompt_version 0)
    architect/v2.md         # Legacy local models
    architect/v3.md         # Legacy tuned for small/local models
    architect/v4.md         # Previous local default, kept as A/B baseline (single-claude/single-gpt architect)
    architect/v5.md         # Current local models — pinned by every local preset; auto for local/mock roles
    implementer/v1.md ... v5.md
    reviewer/v1.md ... v5.md
    director/v1.md          # Director arbitration (runs on the architect's model)
    meta_architect/v1.md    # Project mode: split a project into milestones
    truncation_retry/v1.md  # Appended to a retry after output was cut off at max_tokens
    validation_retry/v1.md  # Appended to a retry after output failed to parse or validate
templates/                  # 30 built-in task templates (JSON)
tests/
    unit/                   # Fast, isolated tests
    integration/            # End-to-end pipeline tests
    conftest.py             # Shared fixtures
schemas/                    # Standalone JSON schema files (for API validation)
examples/
    delegatron.toml         # Example project-level config: roles, pipeline, security, constraints
    golden-path/            # Standard integration test task
    bug-fix/                # Bug fix example
    cli-tool/               # CLI tool example
    react-component/        # React component example
    security-review/        # Security review example
docs/
    user-guide.md           # CLI and configuration guide
    api-reference.md        # REST API endpoints
    install.md              # Installation instructions
    deployment.md           # Production deployment
    security.md             # Security subsystems
    capabilities.md         # Capabilities and limitations (from real runs)
    plugins.md              # Plugin system guide
    architecture/           # Architecture decision records
    deployment/             # Deployment guides (e.g. Raspberry Pi)
dashboard/                  # Next.js React dashboard (separate npm package)
helm/                       # Kubernetes Helm charts
site/                       # Landing page (delegatron.com)
Dockerfile                  # API server image (uvicorn; workspace, runs and data under /app)
docker-compose.yml          # API server with workspace/runs/data volumes
```

## Coding Conventions

- Use type hints on all function signatures
- Use Pydantic v2 models for all structured data (schemas)
- Use `async/await` for all LLM API calls and sandbox operations
- Prefer explicit error types over bare exceptions
- Keep functions small and focused; one responsibility per function
- Use `ruff` rules: `E`, `F`, `W`, `I` (isort), `UP` (pyupgrade), `B` (bugbear), `SIM`, `RUF`; line length 100
- Target 90%+ test coverage on core orchestrator logic

## Agent Communication Protocol

Agents communicate exclusively via structured JSON artifacts:

- **WorkPlan** — Architect outputs: task decomposition, acceptance criteria, termination rules
- **TaskSpec** — Orchestrator-derived: single task for implementer with context
- **PatchSet** — Implementer outputs: whole files, search/replace edits and deletions (or a unified diff), rationale, tests to run
- **TestReport** — Sandbox outputs: pass/fail, test counts, per-test results, failure excerpt, flaky tests, stdout/stderr
- **ReviewReport** — Reviewer outputs: issues by severity, acceptance verdict
- **ArbitrationReport** — Director outputs: one binding ruling (ACCEPT / REVISE / DEFER) per recurring conflict

Never pass free-form chat between agents. All inter-agent messages are schema-validated.

## Key Design Rules

1. **Provider-agnostic** — Any LLM in any role. Adapters: Anthropic, OpenAI, Google, OpenAI-compatible
2. **Orchestrator owns all state** — Models are advisory; only the orchestrator mutates the repo or runs code
3. **Evidence hierarchy for conflicts** — Failing tests > security checks > reviewer blockers > director opinions
4. **Reset context each iteration** — Don't accumulate conversation history; inject fresh context per cycle
5. **Idempotent operations** — Every step must be safely retryable
6. **Budget guardrails always active** — max_iterations, max_cost_usd, max_wall_time enforced at orchestrator level (`max_cost_usd = 0` lifts only the cost cap)
7. **Prompts are external files** — Never hardcode system prompts; load from prompts/ directory for versioning

## Running the Project

```bash
# Install
pip install -e ".[dev]"

# Tests
pytest

# Lint + format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src/

# Run
delegatron run "your task description"
delegatron run --preset budget "your task description"
delegatron run --auto "your task description"       # Auto-select preset by complexity
delegatron run --stream "your task description"     # Stream LLM tokens to stderr
delegatron run --allow-dirty "your task description"  # Start despite uncommitted changes
delegatron run --workspace https://github.com/org/repo.git --branch develop "task"
delegatron run --create-pr "your task description"  # Open a GitHub PR if approved (needs `gh`)
delegatron plan "your task description"
delegatron review                                   # Read-only code review (current dir)
delegatron review --workspace /path/to/repo         # Review specific repo
delegatron review --workspace https://github.com/org/repo.git  # Review from git URL
delegatron review --focus security                  # Security-focused review
delegatron review --with-suggestions                # Review + architect improvement plan
delegatron review --logs /var/log/app.log           # Analyze log files for errors/patterns
delegatron review --logs crash.log --focus bugs     # Focus log analysis on bugs
delegatron refine <run_id>                          # Iteratively refine with user feedback
delegatron retry <run_id>                           # New run of the same request, fed the last review's issues (--fresh: without)
delegatron estimate "your task description"         # Estimate cost before executing
delegatron resume <run_id>                          # Resume from last checkpoint
delegatron show <run_id>                            # Show run summary and artifacts
delegatron list-runs                                # List all saved runs
delegatron list-presets                             # Show available presets
delegatron benchmark "task" --presets default,budget # Compare presets on same task
delegatron swebench --preset local                  # Evaluate on SWE-bench GitHub issues
delegatron project "build a full REST API"          # Multi-milestone project orchestration
delegatron serve                                    # Start the API server
delegatron template list                            # Manage reusable run templates
```

## Environment Variables

Required (only for providers you use):
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

A provider's default key is sent only to that provider's own API host; a role with any other
`base_url` gets no default key (name one with `api_key_env`). Local endpoints need none.

Optional:
- `DELEGATRON_MAX_ITERATIONS` (default: 5)
- `DELEGATRON_MAX_COST_USD` (default: 5.00) — 0 = no cost limit
- `DELEGATRON_MAX_WALL_TIME` (default: 60)
- `DELEGATRON_SANDBOX` (default: docker) — docker, e2b or local (`none` = local)
- `DELEGATRON_LOG_LEVEL` (default: INFO)
- `DELEGATRON_ARCHITECT_MODEL` (default: claude-sonnet-5-5)
- `DELEGATRON_IMPLEMENTER_MODEL` (default: gpt-4.1)
- `DELEGATRON_REVIEWER_MODEL` (default: gemini-2.5-pro)
- `DELEGATRON_DATA_DIR` (default: data) — SQLite DB and server storage directory
- `DELEGATRON_WEBHOOK_SECRET` — GitHub webhook signature verification secret
- `DELEGATRON_DOCKER_IMAGE` — Custom Docker image for sandbox (default: picked from the project's language; e.g. node:22-slim, windows/servercore)
- `DELEGATRON_PARALLEL_TASKS` (default: false) — Generate independent tasks' first patches concurrently (`[pipeline] parallel_tasks`)
- `DELEGATRON_BASELINE_TESTS` (default: true) — Run the workspace's tests once before the first patch (`[pipeline] baseline_tests`)
- `DELEGATRON_CREATE_PR` (default: false), `DELEGATRON_PR_BASE` (default: main), `DELEGATRON_PR_DRAFT` (default: false) — Open a GitHub PR for an approved run (`gh` CLI)
- `DELEGATRON_AUTH_ENABLED` (default: false) — Enable API key authentication on server
- `DELEGATRON_STREAM_SECRET` — Server: secret for signed SSE/download URLs; required when `DELEGATRON_AUTH_ENABLED=true`
- `DELEGATRON_CORS_ORIGINS` (default: localhost:3000, 8001, 8002, 8081) — Server: comma-separated allowed origins
- `DELEGATRON_PROJECT_MODE` (default: false) — Server: enable `POST /api/projects` (multi-milestone project mode)
- `DELEGATRON_APPROVAL_TIMEOUT_MINUTES` (default: 30) — Server: how long an interactive run waits for plan approval before rejecting it
- `DELEGATRON_ALERT_WEBHOOK_URL` — Server: POST webhook when a run ends `error`, `exhausted`, `stalled` or `partial`, or is cancelled
- `DELEGATRON_ELECTRICITY_RATE` (default: 0.12) — Electricity rate in $/kWh for local GPU cost estimation
- `DELEGATRON_GPU_TDP_WATTS` (default: 250) — Fallback GPU power draw when nvidia-smi is unavailable
- `DELEGATRON_GPU_PRICE_USD` (default: 1500) — GPU purchase price for hardware amortization
- `DELEGATRON_GPU_LIFETIME_HOURS` (default: 20000) — Expected GPU lifetime in hours for amortization
- `DELEGATRON_WORKSPACE` — Server: base workspace when the app is served without `delegatron serve` (Docker image)
- `DELEGATRON_ARTIFACTS_DIR` — Server: where run artifacts are stored (default: the install directory's `runs/`)
- `DELEGATRON_WORKSPACE_ROOT` — Server: local workspaces a request may name must lie under it
- `DELEGATRON_GIT_ALLOWED_HOSTS` (default: github.com,gitlab.com,bitbucket.org) — Server: hosts a request's git URL may point at
- `DELEGATRON_ALLOWED_BASE_URLS` — Server: extra role `base_url`s a request may use that still get the server's API keys
- `DELEGATRON_ALLOWED_KEY_ENVS` — Server: extra `api_key_env` names a request may select
- `DELEGATRON_LOCAL_HOSTS` — Extra host names the zero-cloud report counts as local (e.g. a LAN model server's FQDN)
- `DELEGATRON_LOG_DIR` (default: the install directory's `logs/`), `DELEGATRON_LOG_ROTATE` (default: time; or size), `DELEGATRON_LOG_ROTATE_DAYS` (default: 7), `DELEGATRON_LOG_MAX_MB` (default: 100), `DELEGATRON_LOG_TIMEZONE` (default: utc; or local) — Log files and rotation
- `OLLAMA_BASE_URL` (default: http://localhost:11434) — Server: Ollama instance the health, system-stats and defaults endpoints probe
- `OLLAMA_CONTEXT_LENGTH` — Context window assumed for an Ollama model when neither its Modelfile nor the loaded model reports one (else Ollama's 4096)
- `GH_HOST` — `gh`'s GitHub Enterprise host; the zero-cloud report records `--create-pr` API calls to it (default: github.com)
- `E2B_API_KEY` (required if using E2B sandbox)

## Commit Messages

Use conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
