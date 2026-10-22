# Delegatron

## Project status

Delegatron is published by [StoriaBrevi LLC](https://storiabrevi.com) as a **portfolio showcase**.
The code is provided as-is under the [GNU Affero General Public License v3.0](LICENSE), with no
warranty. There is **no support and no installation help**, and **issues and pull requests are
not accepted**. See [delegatron.com](https://delegatron.com) for more about the project.

## Overview

**Multi-agent code bootstrap pipeline. Generates new code from scratch (REST APIs, CLI tools, microservices, test suites). Not a bug-fixer for existing codebases.**

Delegatron treats AI models as specialized team members -- one architects the plan, another writes the code, a third reviews the output. Roles are provider-agnostic: use Claude, GPT, Gemini, Grok, Ollama, or any OpenAI-compatible model in any role. The loop iterates until the code passes all tests, meets acceptance criteria, and clears review -- or hits a budget/iteration guardrail.

**Run it 100% locally with Ollama -- no API keys, no cloud, no API fees. Local quality is limited: works for simple tasks (FizzBuzz, basic APIs), fails on complex tasks (chess, SWE-bench). See capabilities doc.**

> [delegatron.com](https://delegatron.com) | Built by [StoriaBrevi LLC](https://storiabrevi.com)

---

> ⚠️ **Before you use this, read [Capabilities and Limitations](docs/capabilities.md).**
> Local models fail on complex tasks. Cloud models work but cost money. SWE-bench resolution rate is 0%.
> This is a working research project, not a production-ready product.

---

## How It Works

```
You: "Build a FastAPI todo app with auth and tests"
    |
    v
[Architect]  -->  Breaks it into tasks, acceptance criteria, termination rules
    |
    v
[Implementer]  -->  Writes code + tests as structured patches
    |
    v
[Sandbox]  -->  Applies patches, runs tests in Docker/E2B (or locally)
    |
    v
[Reviewer]  -->  Reviews code quality, security, correctness
    |
    v
[Router]  -->  Tests pass + review approved? Done!
    |              Otherwise: loop back and fix
    v
  Git branch with working code + full audit trail
```

Each role can be any LLM. Default: Claude architects, GPT implements, Gemini reviews.
For local: Phi-4 architects + reviews, Qwen2.5-Coder implements.
When the implementer and reviewer keep disagreeing, a director (running on the architect's
model) settles the conflict with a binding ruling.

## Quick Start

### Windows — Clean Install (Nothing Pre-Installed)

Open **PowerShell as Administrator** and paste:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
iwr -useb https://raw.githubusercontent.com/borkit/delegatron/main/bootstrap-windows.ps1 | iex
```

Installs Python, Git, Docker, Ollama, Node.js, clones the repo, and sets up Delegatron (without the cloud-provider SDKs; see Option B). Prompts for optional components.

### Option A: Local with Ollama (no API keys or fees)

```bash
# 1. Install Ollama (https://ollama.com)
# 2. Pull models (needs ~12GB VRAM)
ollama pull phi4:14b
ollama pull qwen2.5-coder:14b

# 3. Install Delegatron
git clone https://github.com/borkit/delegatron.git
cd delegatron
pip install -e .

# 4. Run it in your project's directory
cd /path/to/your/project
delegatron run --preset local "Create a Flask app with a /hello endpoint and tests"
```

### Option B: Cloud APIs

```bash
# 1. Install (the cloud-providers extra adds the Anthropic and Google SDKs)
git clone https://github.com/borkit/delegatron.git
cd delegatron
pip install -e ".[cloud-providers]"

# 2. Set API keys (only for providers you use)
export ANTHROPIC_API_KEY="sk-..."
export OPENAI_API_KEY="sk-..."
export GEMINI_API_KEY="..."

# 3. Run it in your project's directory
cd /path/to/your/project
delegatron run "Add a /health endpoint that returns 200 OK with uptime"
```

A run works on the current directory (or `--workspace <path or git URL>`). In a git
repository it commits to its own branch, `delegatron/run-<run-id>`, and refuses to start
while the repository has uncommitted changes (`--allow-dirty` overrides). Tests run in
Docker by default; without Docker, pass `--sandbox local` (no isolation: tests run on
your machine) or use E2B.

### Option C: Web Dashboard

```bash
# Start the API server (http://localhost:8000, API docs at /docs).
# After `cd dashboard && npm install && npm run build`, it also starts the dashboard on :8001.
delegatron serve

# Or run the dashboard in dev mode:
cd dashboard && npm install && npm run dev
# Open http://localhost:3000
```

## Presets

| Preset | Models | Best For |
|---|---|---|
| `default` | Claude + GPT + Gemini | Best quality (cloud) |
| `claude-heavy` | Claude Opus + Claude Sonnet + Gemini | Complex tasks (cloud) |
| `budget` | All Gemini Flash | Cheapest cloud option |
| `local` | Phi-4:14b + Qwen2.5-Coder:14b + Phi-4:14b | **Default local.** Fits in 12 GB VRAM. Best all-around. |
| `local-phi4` | Phi-4:14b in all roles | Fits in 12 GB VRAM. Fastest local, simple tasks. |
| `local-oss` | Phi-4:14b + gpt-oss:20b + Phi-4:14b | Best local quality (validated 2026-04-18). 13 GB model: fits 12 GB VRAM with modest CPU offload. |
| `local-oss-review` | Phi-4:14b + Qwen2.5-Coder:14b + gpt-oss:20b | Strongest local review, at ~2× reviewer latency. |
| `local-heavy` | Phi-4:14b + Qwen3-Coder:30b + Phi-4:14b | **⚠️ Requires 24 GB+ VRAM.** On smaller cards, 30+ GB spills to system RAM, 3× slower per token, exhausts iteration budget on complex tasks. Only useful on cards that can hold the full 45 GB weights. |
| `single-claude` | All Claude Sonnet | Baseline (cloud) |
| `single-gpt` | All GPT-4.1 | Baseline (cloud) |
| `mock` | Mock providers | Testing (no API keys) |

```bash
delegatron run --preset local "your task"
delegatron run --preset budget "your task"
```

## CLI Commands

```bash
# Run a full pipeline
delegatron run "your task description"

# Run on another repo, or on a git URL (cloned to ~/.delegatron/repos and updated on reuse;
# --branch picks the branch)
delegatron run --workspace /path/to/repo "your task"
delegatron run --workspace https://github.com/org/repo.git --branch dev "your task"

# Start even though the git workspace has uncommitted changes
delegatron run --allow-dirty "your task"

# Plan only (architect step, no code)
delegatron plan "your task description"

# Interactive mode (approve plan before coding)
delegatron run --interactive "your task"

# Read-only code review (no changes made)
delegatron review                                   # Review current directory
delegatron review --workspace /path/to/repo         # Review specific repo
delegatron review --workspace https://github.com/org/repo.git  # From git URL
delegatron review --focus security                  # Security-focused review
delegatron review --with-suggestions                # Review + improvement plan

# Log file analysis
delegatron review --logs /var/log/app.log           # Analyze log files
delegatron review --logs crash.log --focus bugs     # Focus on bugs

# Refine a completed run with feedback
delegatron refine <run-id>

# Resume a failed/cancelled run
delegatron resume <run-id>

# Re-run a finished run, feeding its unresolved review issues back in (--fresh ignores them)
delegatron retry <run-id>

# View run details
delegatron show <run-id>

# List all runs
delegatron list-runs

# Cost estimate before running
delegatron estimate "your task" --preset local --max-iterations 3

# Benchmark presets against each other
delegatron benchmark "your task" --presets default,budget,local

# Evaluate on SWE-bench (real GitHub issues; needs pip install -e ".[swebench]")
delegatron swebench --preset local --split lite --instances 10

# Multi-milestone project orchestration
delegatron project "Build a full REST API with auth, database, and tests"

# Manage templates
delegatron template save my-api --request "Build a REST API with auth"
delegatron template list
delegatron template run my-api

# Start the web server
delegatron serve
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key | -- |
| `OPENAI_API_KEY` | OpenAI API key | -- |
| `GEMINI_API_KEY` | Google AI API key | -- |
| `DELEGATRON_MAX_ITERATIONS` | Max review loop iterations per task | `5` |
| `DELEGATRON_MAX_COST_USD` | Cost cap per run (`0` = no limit) | `5.00` |
| `DELEGATRON_MAX_WALL_TIME` | Wall-time cap (minutes) | `60` |
| `DELEGATRON_SANDBOX` | Sandbox backend (`docker`, `e2b`, `local`; `none` = `local`) | `docker` |
| `DELEGATRON_DOCKER_IMAGE` | Custom Docker image for sandbox | auto-detect |
| `DELEGATRON_LOG_LEVEL` | Log verbosity | `INFO` |
| `DELEGATRON_BASELINE_TESTS` | Run the workspace's tests once before the first patch, so failures that already existed are not blamed on the run | `true` |
| `DELEGATRON_LOCAL_HOSTS` | Extra host names (comma-separated) the zero-cloud report counts as local | -- |
| `DELEGATRON_ARCHITECT_MODEL` | Override architect model | `claude-sonnet-5-5` |
| `DELEGATRON_IMPLEMENTER_MODEL` | Override implementer model | `gpt-4.1` |
| `DELEGATRON_REVIEWER_MODEL` | Override reviewer model | `gemini-2.5-pro` |
| `DELEGATRON_DATA_DIR` | Server storage directory | `data` |
| `DELEGATRON_AUTH_ENABLED` | Enable API key authentication (then `DELEGATRON_STREAM_SECRET` is required) | `false` |
| `DELEGATRON_WORKSPACE` | Server: default workspace when the app is served without `delegatron serve` (e.g. the Docker image) | -- |
| `DELEGATRON_GIT_ALLOWED_HOSTS` | Server: hosts a request's git URL workspace may point at | `github.com,gitlab.com,bitbucket.org` |
| `DELEGATRON_ALLOWED_BASE_URLS` | Server: extra role `base_url`s an API request may use that still get the server's API keys | -- |
| `DELEGATRON_ALLOWED_KEY_ENVS` | Server: extra `api_key_env` names an API request may select | -- |
| `DELEGATRON_ALERT_WEBHOOK_URL` | Server: POST webhook when a run ends `error`, `exhausted`, `stalled` or `partial`, or is cancelled | -- |
| `DELEGATRON_ELECTRICITY_RATE` | Electricity rate ($/kWh) for local GPU cost | `0.12` |
| `DELEGATRON_GPU_TDP_WATTS` | Fallback GPU power draw (watts) | `250` |
| `DELEGATRON_GPU_PRICE_USD` | GPU purchase price for amortization | `1500` |
| `DELEGATRON_GPU_LIFETIME_HOURS` | GPU expected lifetime (hours) | `20000` |

Only the API keys for providers you actually use are required. Ollama needs no keys. A provider's default key is sent only to that provider's own API: an `openai-compatible` role, or one whose `base_url` points at another host, needs `api_key_env` naming its key (see the [Security Guide](docs/security.md#provider-api-keys)). The [User Guide](docs/user-guide.md#environment-variables) lists every variable.

> **Architecture:** All LLM providers use native adapters — no external SDK abstraction
> layers. Ollama/local models use the OpenAI-compatible adapter with v5 prompts and
> JSON repair. Cloud providers (Anthropic, OpenAI, Google) use their respective SDKs.

### Project Config (delegatron.toml)

Place in your project root to customize per-project. `delegatron run` reads
`<workspace>/delegatron.toml`, else `./delegatron.toml`; `--config <path>` names the file
explicitly (the `delegatron.toml` of a repository cloned from a git URL is used only when
`--config` names it).
With `--preset`, a role table here is merged over the preset's role key by key.

```toml
[roles.architect]
provider = "openai-compatible"
model = "phi4:14b"
base_url = "http://localhost:11434/v1"

[roles.implementer]
provider = "openai-compatible"
model = "qwen2.5-coder:14b"
base_url = "http://localhost:11434/v1"

[roles.reviewer]
provider = "openai-compatible"
model = "phi4:14b"
base_url = "http://localhost:11434/v1"

[pipeline]
max_iterations = 3
max_cost_usd = 0          # 0 = no cost limit (local runs still accrue estimated GPU electricity + amortization)
sandbox = "docker"
interactive = false

[constraints]
no_modify = ["alembic/versions/", ".env"]
forbidden_patterns = ["eval(", "exec(", "import os; os.system"]
```

See [examples/delegatron.toml](examples/delegatron.toml) for all options.

## Web Dashboard

The dashboard provides a full UI for managing runs:

- **New Run** -- task description, preset selection, role overrides, Docker image dropdown, constraints
- **Plan Only** -- generate a plan without executing
- **Live Events** -- real-time SSE streaming with planning progress bar and agent reasoning
- **Plan Approval** -- interactive mode with approve/reject from the browser
- **Code Review** -- read-only codebase review and log file analysis
- **Run Comparison** -- side-by-side metrics for any two runs
- **Compliance** -- zero-cloud verification with event log and fingerprint
- **Templates** -- 30 built-in templates plus custom reusable configurations
- **Analytics** -- cost tracking, success rates, preset performance table
- **Live Thinking** -- real-time model reasoning stream, extracts readable content from `<think>` blocks and JSON
- **Iteration History** -- per-iteration timeline with diff viewer, test/review summaries, checkpoint restore
- **Local GPU Costs** -- electricity + hardware amortization for Ollama models instead of $0.00
- **Runs** -- bulk delete, active run indicator, live Ollama VRAM/Docker sparklines on the run page
- **Settings** -- pipeline defaults, API key status, auth management
- **Dark theme** -- the light theme was removed
- **Tooltips** -- descriptive tooltips on all interactive elements

```bash
delegatron serve              # Start API on :8000
cd dashboard && npm run dev   # Start dashboard on :3000
```

## Architecture

```
src/delegatron/
    agents/           # Architect, implementer, reviewer, director + provider adapters (Anthropic, OpenAI, Google, Ollama)
    orchestrator/     # Pipeline loop, routing, termination, arbitration, plan normalization
    sandbox/          # Docker (20+ languages), E2B, and local test runners
    schemas/          # Pydantic models (WorkPlan, PatchSet, TestReport, ReviewReport)
    server/           # FastAPI server with auth, teams (35+ endpoints)
    security/         # Secrets scanning, dependency auditing, import validation, zero-cloud verification
    integrations/     # Git PR creation
    factories/        # Agent, sandbox, and security pipeline factories
    utils/            # Retry, cost tracking, GPU cleanup, logging, context budgeting, test-output parsing
    config.py         # Presets, TOML loading, provider-agnostic config
    workspace.py      # Git workspace management
    worktree.py       # Git worktree isolation for parallel task execution
    detection.py      # Language/framework auto-detection (20+ languages)
    plugins.py        # Plugin system for custom agents
    templates.py      # Run template management (30 built-in)
    autoselect.py     # Automatic preset selection based on task complexity
    project.py        # Multi-milestone project orchestration
    swebench.py       # SWE-bench evaluation on real GitHub issues
    repomap.py        # Repository structure mapping and file significance
```

## Key Design Principles

- **Structured artifacts, not chat** -- Agents communicate via JSON schemas, not free-form text
- **Evidence over opinion** -- Failing tests override agent opinions; deterministic checks dominate
- **Hub-spoke orchestration** -- Central orchestrator owns all state; models are advisory only
- **Budget-aware termination** -- Max iterations, cost caps, wall-time limits, stall detection
- **Full provenance** -- Every decision, API call, and artifact is logged
- **Provider-agnostic** -- Any LLM in any role, including local models via Ollama

## Development

```bash
pip install -e ".[dev]"

# Tests
pytest

# Lint
ruff check src/ tests/

# Type check
mypy src/ --ignore-missing-imports

# Format
ruff format src/ tests/
```

## Troubleshooting (Ollama)

| Error | Fix |
|---|---|
| "Cannot connect to Ollama" | Run `ollama serve` first |
| "Model not found" | Run `ollama pull <model-name>` |
| "Out of memory" | Close other apps; the 14B models of `local` fit in 12 GB VRAM, 30B models (`local-heavy`) need 24 GB+ |
| Invalid JSON from model | Normal with local models -- Delegatron retries automatically |
| Slow response | A model that does not fit in VRAM spills to system RAM and runs ~3× slower per token: 30B models need 24 GB+ VRAM; 14B need ~9 GB; 8B models are faster |
| Prompt cut off / model ignores instructions | Ollama's default 4096-token window is small; see [Context window](docs/install.md#context-window-ollama_context_length--num_ctx) |
| "The Docker sandbox needs the docker CLI" | Install Docker, or run with `--sandbox local` (no isolation) or `--sandbox e2b` |

## License

Copyright (C) 2026 StoriaBrevi LLC

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, version 3.
It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE)
for the full text.

No support is offered. Use at your own risk. See [docs/capabilities.md](docs/capabilities.md) for what works and what doesn't.

## Links

- [Capabilities and Limitations](docs/capabilities.md)
- [Roadmap](ROADMAP.md)
- [Changelog](CHANGELOG.md)
- [Installation Guide](docs/install.md)
- [User Guide](docs/user-guide.md)
- [API Reference](docs/api-reference.md)
- [Architecture Docs](docs/architecture/)
- [Deployment Guide](docs/deployment.md)
- [Security Guide](docs/security.md)
- [Plugin Guide](docs/plugins.md)
- [Security Policy](SECURITY.md)
