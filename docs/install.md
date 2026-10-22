# Delegatron Quick Start

Get from zero to running a multi-agent coding pipeline in under 5 minutes.

## Windows — Clean Install (Nothing Pre-Installed)

If you have a fresh Windows 10/11 machine with nothing installed, use the bootstrap script. Open **PowerShell as Administrator** and run:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
iwr -useb https://raw.githubusercontent.com/borkit/delegatron/main/bootstrap-windows.ps1 | iex
```

This installs everything in order: Python, Git, Docker Desktop, Ollama, Node.js, clones the repo, creates a virtual environment, and installs Delegatron (`pip install -e ".[dev]"`, which leaves out the cloud-provider SDKs; see [Minimal Environment Setup](#minimal-environment-setup)). It prompts (Y/N) for optional components.

After it finishes:

```powershell
cd ~\delegatron
.venv\Scripts\Activate.ps1
delegatron --version   # verify install
```

Start runs from your own project's directory (or pass `--workspace`), not from `~\delegatron`: a run works on the directory it starts in and, in a git repository, commits to a `delegatron/run-<run-id>` branch there. The mock preset needs no API keys, but its tests still run in Docker unless you add `--sandbox local`.

See [bootstrap-windows.ps1](../bootstrap-windows.ps1) for details.

## Install from Source (Linux/macOS/Windows with Git)

If you already have Python 3.11+ and Git installed:

```bash
git clone https://github.com/borkit/delegatron.git
cd delegatron
pip install -e ".[dev]"
```

Verify:

```bash
delegatron --version
```

### Optional: E2B sandbox support

If you want microVM sandboxing instead of Docker:

```bash
pip install -e ".[e2b]"
export E2B_API_KEY=your_key
export DELEGATRON_SANDBOX=e2b
```

## Minimal Environment Setup

The Anthropic and Google SDKs are an optional extra. Install it for any preset that uses Claude or Gemini (every cloud preset except `single-gpt`):

```bash
pip install -e ".[cloud-providers]"
```

You only need **one** provider API key. The easiest path is using the `budget` preset, which only needs Google Gemini:

```bash
export GEMINI_API_KEY=your_gemini_key
```

For the default preset (Claude + GPT + Gemini), set all three:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
```

You can also put these in a `.env` file in the root of your Delegatron checkout (next to `pyproject.toml`) -- Delegatron loads it automatically via `python-dotenv`, and its values override variables already set in the shell.

A provider's default key is sent only to that provider's own API. An `openai-compatible` role, or one whose `base_url` points at another host, needs `api_key_env` naming its key (see the [Security Guide](security.md#provider-api-keys)).

## Run Your First Task

```bash
# Budget mode (single provider):
delegatron run --preset budget "Add a hello world endpoint to app.py"

# Default mode (three providers):
delegatron run "Add a hello world endpoint to app.py"

# Plan only (no code changes):
delegatron plan "Refactor the auth module"

# Interactive mode (approve the plan before coding starts):
delegatron run --interactive "Add input validation to the API"

# Mock mode (no API keys needed, for testing the pipeline):
delegatron run --mock "Add a hello world endpoint"

# Read-only code review (no changes made):
delegatron review
delegatron review --workspace https://github.com/org/repo.git
delegatron review --focus security --with-suggestions

# Log file analysis:
delegatron review --logs /var/log/app.log --focus bugs
```

### CLI options

```
--preset        Config preset: default, claude-heavy, budget, local, local-phi4, local-heavy, local-oss, local-oss-review, single-claude, single-gpt, mock
--max-iterations  Max review loop iterations per task (default: 5)
--max-cost      Max spend in USD (default: 5.00; 0 = no limit)
--max-time      Max wall-time in minutes (default: 60)
--sandbox       Sandbox: docker, e2b or local (none = local; default: docker)
--workspace     Path to target repo or git URL (default: current directory)
--branch        Branch to clone when --workspace is a git URL (default: the remote's default)
--allow-dirty   Start even if the git workspace has uncommitted changes
--config        Path to delegatron.toml (default: <workspace>/delegatron.toml, then ./delegatron.toml)
--interactive   Approve plan before coding starts
--json-output   Output results as JSON
--stream        Stream LLM token output to stderr in real-time
--auto          Auto-select preset based on task complexity
--mock          Use mock providers (no API keys needed)
--docker-image  Docker image for the sandbox (default: picked from the project's language)
--create-pr     Open a GitHub PR if the run is approved (needs an authenticated `gh` CLI)
--pr-base       Base branch for that PR (default: main)
--pr-draft      Open that PR as a draft
--log-file      Write JSON-lines logs to this file
--verbose       Enable debug logging

# Review-specific options (review also takes --workspace and --branch):
--focus         Focus area: security, bugs, performance, or all
--with-suggestions  Run architect for improvement suggestions
--logs          Log file(s) to analyze (repeatable)
```

## Run with the Dashboard

Start the API server:

```bash
delegatron serve --host 0.0.0.0 --port 8000 --workspace /path/to/your/repos
```

In a separate terminal, start the dashboard:

```bash
cd dashboard
npm install
npm run dev
```

The dashboard runs at `http://localhost:3000` and connects to the API at `http://localhost:8000`.

Once the dashboard is built (`npm run build` in `dashboard/`), `delegatron serve` also starts it itself, on the port after the API's (`http://localhost:8001` by default).

For production dashboard deployment, see [deployment.md](deployment.md).

## Available Presets

| Preset | Architect | Implementer | Reviewer | Notes |
|---|---|---|---|---|
| `default` | Claude Sonnet (Anthropic) | GPT-4.1 (OpenAI) | Gemini 2.5 Pro (Google) | Needs all 3 keys |
| `claude-heavy` | Claude Opus (Anthropic) | Claude Sonnet (Anthropic) | Gemini 2.5 Pro (Google) | Needs Anthropic + Google |
| `budget` | Gemini Flash (Google) | Gemini Flash (Google) | Gemini Flash (Google) | Only needs Google key |
| `local` | phi4:14b (local) | qwen2.5-coder:14b (local) | phi4:14b (local) | Free, 12GB VRAM, default local preset |
| `local-phi4` | phi4:14b (local) | phi4:14b (local) | phi4:14b (local) | Free, fastest single-model |
| `local-oss` | phi4:14b (local) | gpt-oss:20b (local) | phi4:14b (local) | Free, best local quality (validated 2026-04-18); 13 GB model, modest CPU offload on 12 GB cards |
| `local-oss-review` | phi4:14b (local) | qwen2.5-coder:14b (local) | gpt-oss:20b (local) | Free, strongest local review (~2× reviewer latency) |
| `local-heavy` | phi4:14b (local) | qwen3-coder:30b (local) | phi4:14b (local) | **⚠️ Requires 24 GB+ VRAM.** Spills 30+ GB to system RAM on smaller cards, 3× slower per token, exhausts iteration budget on complex tasks. |
| `single-claude` | Claude Sonnet (Anthropic) | Claude Sonnet (Anthropic) | Claude Sonnet (Anthropic) | Single-agent cloud |
| `single-gpt` | GPT-4.1 (OpenAI) | GPT-4.1 (OpenAI) | GPT-4.1 (OpenAI) | Single-agent cloud |
| `mock` | Mock | Mock | Mock | No API keys needed |

List presets: `delegatron list-presets`

### Optional: Local models with Ollama (free, no API keys)

Install [Ollama](https://ollama.com) and pull the models of the `local` preset:

```bash
ollama pull phi4:14b            # architect + reviewer
ollama pull qwen2.5-coder:14b   # implementer
delegatron run --preset local "Add a hello world endpoint"
```

The other local presets need their own models: `gpt-oss:20b` (`local-oss`, `local-oss-review`) and `qwen3-coder:30b` (`local-heavy`).

A role counts as local when its `base_url` points at this machine or the local network, such as an Ollama server on another LAN host (`base_url = "http://192.168.1.100:11434/v1"`). Unless the role sets `prompt_version` or `timeout_seconds`, a local role gets the v5 prompts written for small models and a 900-second request timeout (cloud roles: v1 prompts, 600 seconds). It is sent no API key unless it sets `api_key_env` (or `api_key`).

#### Context window (`OLLAMA_CONTEXT_LENGTH` / `num_ctx`)

Ollama runs every model with a fixed context window, 4096 tokens unless you configure it. A prompt that does not fit is cut silently, so the model can lose its instructions. Delegatron therefore checks each prompt against the window and, when it is too long, trims context in this order:

1. the repository map;
2. related and unchanged files that the attempt does not edit;
3. the files the attempt must edit: the task's outputs, files named by failing tests or review feedback, and files in the previous patch. These are never dropped. They are shortened to their beginning and end around a truncation marker. The implementer may change such an excerpt only through search/replace `edits`; the orchestrator refuses a full rewrite that would lose the part the model did not see.

Each trim is logged as a `WARNING` and recorded in the run's `events.jsonl` as a `context_trimmed` event that names the role and the trimmed parts, for example:

```json
{"event": "context_trimmed", "role": "implementer", "iteration": 3, "keys": ["repo_map", "related_files"]}
```

Delegatron sizes the window only for an Ollama server. A role's endpoint is probed when it could be Ollama: a `base_url` on this machine or a private network (`localhost`, `127.0.0.1`, `::1`, `host.docker.internal`, private, link-local and CGNAT IPv4 addresses, IPv6 unique-local and link-local addresses, single-label host names such as a Compose service `ollama`, and host names ending in `.localhost`, `.local`, `.lan`, `.internal` or `.home.arpa`), or one that contains port `11434` or `ollama`. Delegatron first calls `GET /api/version`. Only when the server answers like Ollama does it take the first of these that is set:

1. the model's Modelfile `num_ctx` (read through `/api/show`, which does not load the model)
2. the context length the loaded model runs with (`/api/ps`)
3. `OLLAMA_CONTEXT_LENGTH` in Delegatron's own environment
4. 4096, Ollama's default

The result never exceeds the model's trained context length. Unless the Modelfile sets `num_ctx`, the window is read again from `/api/ps` once after the first successful request, when the model is loaded.

Any other server has an unknown window, and its prompts are sent without trimming. This covers LM Studio, vLLM, llama.cpp, a LiteLLM proxy and cloud APIs. Delegatron logs a `WARNING` when a local server does not answer like Ollama. Set `context_window` for such a role (see below) to turn trimming on.

Delegatron talks to Ollama through its OpenAI-compatible `/v1` endpoint, which has no per-request `num_ctx` option. Delegatron therefore cannot enlarge the window itself: configure it on the Ollama side. A 4096-token window leaves little room for code. Give local models more context in one of these ways (more context uses more VRAM):

- **Server-wide:** set `OLLAMA_CONTEXT_LENGTH` for the Ollama server and restart it. Detection reads the new size from the loaded model. Set the same value where you run Delegatron as well, so the first request, sent before the model is loaded, is sized correctly too.

  ```bash
  export OLLAMA_CONTEXT_LENGTH=16384   # Linux/macOS, then restart `ollama serve`
  ```

  ```powershell
  # Windows: set it for your user, then quit and restart the Ollama app
  [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "16384", "User")
  ```

- **Per model:** create a variant with a larger `num_ctx` and use its name in your config.

  ```bash
  printf 'FROM qwen2.5-coder:14b\nPARAMETER num_ctx 16384\n' > Modelfile
  ollama create qwen2.5-coder-16k -f Modelfile
  ```

- **Per role in `delegatron.toml`:** set the window explicitly. This skips detection. It must match what the server really uses. Set it for local servers other than Ollama too, or their prompts are never trimmed.

  ```toml
  [roles.implementer]
  context_window = 16384
  ```

## Troubleshooting

### `ModuleNotFoundError: No module named 'delegatron'`

You need to install the package:

```bash
pip install -e ".[dev]"
```

### `ModuleNotFoundError: No module named 'e2b_code_interpreter'`

Install the E2B extra:

```bash
pip install -e ".[e2b]"
```

### `Anthropic SDK not installed` / `Google GenAI SDK not installed`

Install the cloud-provider extra:

```bash
pip install -e ".[cloud-providers]"
```

### Docker sandbox errors / "Cannot connect to Docker daemon"

Make sure Docker is installed and running:

```bash
docker info
```

If Docker is not running, start it (`sudo systemctl start docker` on Linux, or open Docker Desktop on macOS/Windows).

If you do not have Docker, the run stops with "The Docker sandbox needs the docker CLI". Switch to the E2B sandbox, or to the local sandbox, which runs the tests on this machine without isolation (`--mock` changes only the models, not the sandbox):

```bash
delegatron run --sandbox e2b "your task"
# or
delegatron run --sandbox local "your task"
```

### `Workspace setup failed: uncommitted changes in the workspace`

In a git repository a run checks out and commits to its own branch, `delegatron/run-<run-id>`, so it refuses to start while the repository has uncommitted changes (Delegatron's own directories, such as `runs/` and `.delegatron-runs/`, do not count). Commit or stash them, or pass `--allow-dirty` to start anyway:

```bash
delegatron run --allow-dirty "your task"
```

### API key errors ("Invalid API key", 401)

Check that the correct environment variable is set for your provider:

```bash
echo $ANTHROPIC_API_KEY   # Should start with sk-ant-
echo $OPENAI_API_KEY      # Should start with sk-
echo $GEMINI_API_KEY
```

A 401 from a custom `base_url` usually means the role was sent no key: the default variables go only to their provider's own API (Delegatron logs a `WARNING` starting `Not sending ...`). Name the role's variable with `api_key_env` in `delegatron.toml`.

### `Connection refused` when using the dashboard

Make sure the API server is running:

```bash
delegatron serve --port 8000
```

And that the dashboard is configured to connect to it. For local dev, the default (`http://localhost:8000`) should work. For production, set `NEXT_PUBLIC_API_URL` at build time.

### Runs time out or hit cost limits

Increase the limits:

```bash
delegatron run --max-iterations 10 --max-cost 20.0 --max-time 120 "your task"
```

Or set via environment variables:

```bash
export DELEGATRON_MAX_ITERATIONS=10
export DELEGATRON_MAX_COST_USD=20.0
export DELEGATRON_MAX_WALL_TIME=120
```

A preset's or `delegatron.toml`'s own limits win over these variables (the local presets set all three); the CLI flags win over both.
