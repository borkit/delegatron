# Delegatron User Guide

## What is Delegatron?

Delegatron is a multi-agent coding pipeline. You describe what you want in natural language, and it orchestrates multiple LLMs to plan, implement, test, and review the code — producing tested, reviewed commits on a run branch (and a pull request with `--create-pr`).

## Quick Start

### Installation

```bash
pip install -e ".[dev]"                   # OpenAI, OpenAI-compatible endpoints, Ollama
pip install -e ".[dev,cloud-providers]"   # + the Anthropic and Google SDKs (default preset)
```

Add the `e2b` extra for the E2B sandbox and `swebench` for `delegatron swebench`.

### Set API Keys

Set the key of every provider your preset uses (the default preset uses all three):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # For Claude (architect)
export OPENAI_API_KEY="sk-..."          # For GPT (implementer)
export GEMINI_API_KEY="..."             # For Gemini (reviewer)
```

### Run Your First Task

```bash
delegatron run "Add a /health endpoint that returns JSON {status: ok}"
```

### Test Without API Keys

Use the mock provider for testing:

```bash
delegatron run --preset mock "Add a health endpoint"
delegatron run --mock --sandbox local "Add a health endpoint"   # no Docker either
```

## CLI Reference

### `delegatron run`

Execute a full plan/code/test/review pipeline.

```bash
delegatron run [OPTIONS] REQUEST

Options:
  --preset TEXT         Config preset (default, claude-heavy, budget, mock, local,
                        local-phi4, local-heavy, local-oss, local-oss-review,
                        single-claude, single-gpt)
  --max-iterations INT  Max iterations per task (default: 5)
  --max-cost FLOAT      Max cost in USD (default: 5.00; 0 = no limit)
  --max-time INT        Max wall-time in minutes (default: 60)
  --sandbox TEXT        Sandbox type: docker, e2b, local or none (= local)
                        (default: docker)
  --docker-image TEXT   Docker image for the sandbox (e.g. node:22-slim;
                        default: auto-selected from the detected language)
  --workspace PATH      Path to target repo or git URL (default: current directory)
  --branch TEXT         Branch to clone when --workspace is a git URL
                        (default: the remote's default branch); also the base
                        the run branch forks from
  --config PATH         Path to delegatron.toml (default: <workspace>/delegatron.toml,
                        then ./delegatron.toml; a repository cloned from a git
                        URL is read only when --config names its file)
  --allow-dirty         Start even if the git workspace has uncommitted changes
  --interactive         Approve the architect's plan before coding starts
  --json-output         Output structured JSON instead of pretty text
  --log-file PATH       Write JSON-lines logs to this file (instead of
                        logs/delegatron.log)
  --mock                Use mock providers (no API keys needed)
  --stream              Stream LLM token output to stderr in real-time
  --auto                Auto-select preset based on task complexity
  --create-pr           Open a GitHub PR if the run is approved
  --pr-base TEXT        Base branch for PR (default: main)
  --pr-draft            Open the PR as a draft
  --verbose, -v         Enable debug logging
```

In a git repository the run works on its own branch, `delegatron/run-<run-id>`,
created from the base branch you asked for (`--branch`), else from the branch
you have checked out (the repository's default branch only when HEAD is
detached). A new run whose branch name is already taken gets
`delegatron/run-<run-id>-<n>`; only `resume` and `refine` continue on an
existing run branch. Every applied patch is committed there, and the run's
result shows the branch and how to return to your previous checkout.
A run refuses to start when the repository has uncommitted changes (they would
end up mixed with the run's commits); commit or stash them, or pass
`--allow-dirty`. Delegatron's own directories (`runs/`, `reviews/`, `_sandbox/`,
`.delegatron-*`) never count as changes.

A run ends with one of these statuses:

| Status | Meaning |
|---|---|
| `approved` | Every task passed its tests and review |
| `partial` | Some tasks were approved; others were given up (stalled or out of their iterations), or an accepted test fails or is gone at the end. No PR is opened |
| `exhausted` | The iteration, cost or wall-time budget ran out, also after some tasks were approved (`resume` continues with the tasks left) |
| `stalled` | The last pending task made no progress (see [Pipeline stalls](#pipeline-stalls)) |
| `error` | The architect produced no plan, the implementer or reviewer failed twice in a row, the implementer reported it is blocked, the workspace or sandbox could not be prepared, or the plan was rejected |

A server run that is cancelled (from the dashboard or `POST /api/runs/{run_id}/cancel`)
ends `cancelled`. `summary.json` lists each task's outcome under `task_outcomes`.

The wall-time limit (`--max-time`, `max_wall_time_minutes`) is a hard
deadline: every agent call, sandbox run and the pre-run test baseline runs
under the time that is left and is cancelled when it runs out, and no call
starts once no time is left. The run then ends `exhausted` and still writes
`summary.json`, its final checkpoint and the zero-cloud report. Time used is
checkpointed, so `resume` continues the same budget; each `refine` round gets
a fresh one.

#### Run results (`runs/<run-id>/`)

`summary.json` holds, besides the status and `task_outcomes`:

| Field | Meaning |
|---|---|
| `termination_reason` | Which limit stopped the run, e.g. `Iteration limit (5) reached`, `Cost limit ($5.00) reached: $5.12 spent`, `Wall time limit (60min) reached` |
| `total_cost_usd` | Cost of every session of the run; `prior_cost_usd` and `session_cost_usd` split it (`cost_breakdown` has this session's calls, tokens and cost per role, and the local GPU cost) |
| `elapsed_seconds` | Wall time of every session of the run; `prior_elapsed_seconds` and `session_elapsed_seconds` split it |
| `branch` | The run branch (empty without git) |
| `roles` | Each role's provider, model, `base_url` and resolved `prompt_version` (no keys) |
| `files_changed` / `files_deleted` | Paths the run left changed, and those it deleted |
| `skipped_tasks` | Every task the run did not deliver (given up, stopped, or never reached) |
| `baseline_failures` | Tests that already failed before the run (`{test id: origin}`), from the pre-run baseline |
| `fixed_baseline_failures` | Pre-existing failures the run fixed |
| `regressed_tests` | Accepted tests of an approved task that fail again at the end, or that a task given up later removed (`{test id: task}`); such a run is `partial` |
| `flaky_tests` | Every test the run saw pass and fail without a code change, with its first evidence, owner and `detected_iteration`; `resolved_iteration` is set when its test file changed after that (for example, made deterministic) |
| `security` | Security mode and the findings still open after a final scan of everything the run changed: `unresolved_findings`, counts by kind and severity, `unresolved`, `unresolved_blocking`, `detected`, `blocked_patches` |
| `zero_cloud` | The zero-cloud verdict: `verified`, `violations`, `fingerprint` (the full report is `zero_cloud_report.json`) |
| `pull_request` | With `--create-pr`: `created` and `url`/`number`, or `error`/`skipped_reason` |

It also records `run_id`, `request`, `plan_summary`, `iterations`, `is_approved`,
`error`, `workspace_root` and `observer_metrics`; `refine` adds
`refinement_rounds`.

Next to it are one `state_NNN.json` checkpoint per iteration, the
`work_plan.json` (re-plans as `work_plan_r1.json`, `work_plan_r2.json`, ...),
`patch_set_NNN.json`, `test_report_NNN.json` and `review_report_NNN.json` per
iteration, `arbitration_NNN.json` when the director ruled, the
`baseline_test_report.json` of the pre-run test baseline, `provider_calls.jsonl`
(one cost row per agent call), `zero_cloud_report.json` and `events.jsonl`.
A test report whose `skipped_reason` is set ran no test: `non_testable_language`,
`config_only_patch` (the attempt wrote only configuration), `security_blocked`
(block mode kept the patch out) or `paths_rejected` (every path was unsafe).
`events.jsonl` records every step; notable events are `context_trimmed` (a
prompt cut to fit the model's window: role, keys, token counts, dropped and
excerpted files), `baseline_tests` / `baseline_tests_failed`,
`flaky_tests_detected`, `flaky_tolerated`, `replanned` (with `kept_completed`,
`dropped_completed` and `reworked_skipped`: tasks given up that the new plan
changed, so they are attempted again), `replan_rejected` (with a `reason`),
`replan_suppressed`, `tasks_reopened` (a resume found every task finished and
retries those given up), `terminated` (`limit`, `phase`,
`iteration`, `elapsed_seconds`), `workspace_not_git` (a folder its enclosing
repository ignores runs without git) and `implementer_unavailable` /
`reviewer_unavailable` (an agent call failed, the run carries on).

Before the first patch the run executes the workspace's tests once
(`[pipeline] baseline_tests`, default `true`, or `DELEGATRON_BASELINE_TESTS`);
what already fails then is pre-existing and never blamed on the run. A failing
test may be rerun up to `flaky_reruns` times (default 2, at most 5) to tell a
flaky test from a real failure; with `tolerate_flaky_tests = true` a run whose
every failure is a pre-existing test that passed on a rerun counts as passed
(`raw_passed` keeps the real result). A test in a file the run wrote is never
tolerated.

#### GitHub PR creation (`--create-pr`)

When enabled, the pipeline opens a pull request on GitHub after a
successful (`approved`) run. The PR body includes an audit summary:
plan, iteration count, cost breakdown, task outcomes, reviewer verdict, and
acceptance criteria.

```bash
# One-off:
delegatron run --preset local --create-pr --workspace /path/to/repo \
  "Add a /health endpoint"

# Custom base branch + draft:
delegatron run --create-pr --pr-base develop --pr-draft "Refactor auth"

# Enable globally via env var:
export DELEGATRON_CREATE_PR=true
export DELEGATRON_PR_BASE=main
export DELEGATRON_PR_DRAFT=false
```

Requirements:
- `gh` CLI installed and authenticated (`gh auth login`)
- Workspace is a git repo with an `origin` remote
- Run must finish with status `approved` — PRs are never opened for
  partial/exhausted/stalled/error runs, nor while a commit failed or a
  blocking security finding is unresolved

Failure handling: if `gh` is missing, unauthenticated, or PR creation
fails for any reason, the run is NOT failed. The error is logged and
recorded in `summary.json` under `pull_request.error` /
`pull_request.skipped_reason`.

### `delegatron plan`

Generate a work plan without executing it.

```bash
delegatron plan "Refactor the auth module to use JWT tokens"
```

### `delegatron resume`

Resume a previously checkpointed run.

```bash
delegatron resume <run-id>
```

The run continues on its run branch from the last checkpoint (written after the
plan, after every iteration and when the run ends): completed tasks stay
completed, and the cost and wall time in `summary.json` add up across
sessions. A task the last session gave up is retried. When the last session
already approved its current task, the run goes on with the next pending task;
with none left, the tasks given up are retried (`tasks_reopened`). An approved
task is never run again. `resume` takes `--workspace`, `--preset`, `--config`,
`--mock` and `--allow-dirty` like `run`.

### `delegatron retry`

Start a run again under a new run ID (and a new run branch), with the same
request. Unless `--fresh` is given, the issues of the previous run's last review
are appended to the request so the new attempt focuses on fixing them.

```bash
delegatron retry <run-id>
delegatron retry <run-id> --fresh
delegatron retry <run-id> --preset budget
```

### `delegatron refine`

Iteratively refine a completed run with user feedback. Loads the artifacts from a previous run, shows the current state (plan, code, tests, review), then enters a loop where you provide feedback. Each round re-opens the run's current task (it is no longer completed or skipped) and re-runs the implementer, sandbox, and reviewer with the feedback incorporated; each round gets its own iteration and wall-time budget. When you finish (`done`), `summary.json` is updated with the final state and `refinement_rounds`.

```bash
delegatron refine <run-id>
delegatron refine <run-id> --preset budget
delegatron refine <run-id> --mock
```

### `delegatron estimate`

Estimate the cost of a pipeline run before executing. Shows a per-role cost breakdown and a total range based on assumed iterations.

```bash
delegatron estimate "Add input validation to the API"
delegatron estimate --preset claude-heavy --max-iterations 5 "Refactor auth module"
```

### `delegatron show`

Show the summary and artifacts for a completed run, including cost breakdown by role, iteration count, the limit that stopped it (`termination_reason`), each task's outcome, regressed tests and fixed pre-existing failures, the run branch, the files changed and deleted, and a list of saved artifacts.

```bash
delegatron show <run-id>
delegatron show <run-id> --workspace /path/to/repo
```

### `delegatron list-runs`

List all pipeline runs in the workspace with their status, iteration count, and cost.

```bash
delegatron list-runs
delegatron list-runs --workspace /path/to/repo
```

### `delegatron list-presets`

List all available configuration presets with their role/model assignments.

```bash
delegatron list-presets
```

### `delegatron review`

Read-only code review or log file analysis. No code changes are made.

```bash
# Review current directory
delegatron review

# Review a specific repo or git URL
delegatron review --workspace /path/to/repo
delegatron review --workspace https://github.com/org/repo.git

# Focus on a specific area
delegatron review --focus security
delegatron review --focus bugs
delegatron review --focus performance

# Review with architect improvement suggestions
delegatron review --with-suggestions

# Analyze log files
delegatron review --logs /var/log/app.log
delegatron review --logs crash.log --focus bugs
```

Options:
- `--workspace PATH` — Local path or git URL (default: current directory)
- `--branch TEXT` — Branch to clone when `--workspace` is a git URL (default: the remote's default branch)
- `--focus TEXT` — Focus area: `security`, `bugs`, `performance`, or `all`
- `--with-suggestions` — Also run the architect to produce an improvement plan
- `--logs FILE` — Analyze log files instead of source code (repeatable)
- `--preset TEXT` — Config preset (only reviewer model is used, plus the architect's with `--with-suggestions`)
- `--config PATH` — Path to `delegatron.toml` (a cloned repository's own file is used only when named here)
- `--mock` — Use mock providers
- `--json-output` — Machine-readable JSON output

### `delegatron benchmark`

Run the same task with multiple presets and compare results (quality score, cost, speed).

```bash
delegatron benchmark "Add a health endpoint" --presets default,budget,mock
```

`--presets` defaults to `default,budget,mock`; `--workspace` and
`--json-output` work as for `run`.
Each preset's run gets its own run branch, forked from the same base. Like
`run`, a benchmark refuses a git workspace with uncommitted changes unless
`--allow-dirty` is given.

### `delegatron swebench`

Evaluate Delegatron on real GitHub issues from the [SWE-bench](https://www.swebench.com/) dataset. Runs multiple instances and reports resolution rates by complexity tier.

⚠️ **SWE-bench evaluation is functional but resolution rate is 0%.** Local models cannot parse complex GitHub issues. Cloud models haven't been tested on enough instances to claim any resolution rate. Use this for infrastructure testing only.

```bash
delegatron swebench --preset local --split lite --instances 10
delegatron swebench --preset default --split lite --tier simple --instances 5
delegatron swebench --preset local --json-output
```

Options:
- `--preset TEXT` — Config preset (default: `default`)
- `--split TEXT` — SWE-bench split: `lite`, `verified`, or `full` (default: `lite`)
- `--instances INT` — Number of instances to run (default: 10)
- `--tier TEXT` — Filter by complexity: `simple`, `moderate`, `complex`, `feature`
- `--max-iterations INT` — Max iterations per instance (default: 5)
- `--max-cost FLOAT` — Max cost per instance in USD (default: 5.0)
- `--work-dir PATH` — Working directory for the cloned repositories (default: `swebench-workdir`)
- `--json-output` — Machine-readable JSON output

Requires the `swebench` extra (`pip install -e ".[swebench]"`).

### `delegatron project`

Orchestrate multi-milestone projects. Decomposes a high-level request into milestones, runs each through the full pipeline, and accumulates code across milestones. Respects global cost and time budgets.

```bash
delegatron project "Build a full REST API with auth, database, and tests"
delegatron project --max-milestones 5 --max-cost 50.0 "Refactor the monolith into microservices"
```

Options:
- `--preset TEXT` — Config preset
- `--workspace PATH` — Target repo (default: current directory)
- `--max-milestones INT` — Maximum milestones (default: 10)
- `--max-iterations INT` — Max iterations per milestone (default: 5)
- `--max-cost FLOAT` — Total cost cap across all milestones (default: 50.0)
- `--max-time INT` — Total wall-time cap in minutes (default: 120)
- `--allow-dirty` — Start even if the git workspace has uncommitted changes
- `--json-output` — Machine-readable JSON output
- `--verbose, -v` — Enable debug logging

Each milestone's run branch forks from the branch the previous milestone used.

### `delegatron template`

Manage run templates for repeatable configurations.

```bash
delegatron template list
delegatron template save my-template --request "Add {feature}" --description "Standard feature workflow"
delegatron template run my-template --var feature="caching"
delegatron template delete my-template
```

`template run` also takes `--workspace`, `--preset` (overrides the template's
own), `--create-pr`, `--mock`, `--json-output` and `--allow-dirty`. Templates
are stored in `~/.delegatron/templates` (`--dir` for another store); the
built-in ones come from `templates/`.

### `delegatron serve`

Start the API server (and the dashboard, when it is built) for a workspace.

```bash
delegatron serve                                  # API on :8000, dashboard on :8001
delegatron serve --host 127.0.0.1 --port 9000 --workspace /path/to/repo
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DELEGATRON_MAX_ITERATIONS` | 5 | Maximum iterations per task |
| `DELEGATRON_MAX_COST_USD` | 5.00 | Cost budget per run (`0` = no limit) |
| `DELEGATRON_MAX_WALL_TIME` | 60 | Wall time limit in minutes |
| `DELEGATRON_SANDBOX` | docker | Sandbox type (docker/e2b/local; none = local) |
| `DELEGATRON_LOG_LEVEL` | INFO | Log verbosity |
| `DELEGATRON_ARCHITECT_MODEL` | claude-sonnet-5-5 | Override architect model |
| `DELEGATRON_IMPLEMENTER_MODEL` | gpt-4.1 | Override implementer model |
| `DELEGATRON_REVIEWER_MODEL` | gemini-2.5-pro | Override reviewer model |
| `DELEGATRON_DOCKER_IMAGE` | auto-detect | Custom Docker image for sandbox |
| `DELEGATRON_PARALLEL_TASKS` | false | Generate ready tasks' first patches concurrently (`[pipeline] parallel_tasks`) |
| `DELEGATRON_CREATE_PR` | false | Open a GitHub PR after an approved run (`[pipeline] create_pr`) |
| `DELEGATRON_PR_BASE` | main | Base branch for that PR (`[pipeline] pr_base`) |
| `DELEGATRON_PR_DRAFT` | false | Open that PR as a draft (`[pipeline] pr_draft`) |
| `DELEGATRON_DATA_DIR` | data | SQLite DB + server storage directory |
| `DELEGATRON_WEBHOOK_SECRET` | — | GitHub webhook signature verification |
| `DELEGATRON_AUTH_ENABLED` | false | Enable API key auth on the server (then `DELEGATRON_STREAM_SECRET` is required) |
| `DELEGATRON_STREAM_SECRET` | random per process (auth off) | Server: key that signs SSE/download stream tokens |
| `DELEGATRON_WORKSPACE` | — | Server: workspace when the app is served directly by uvicorn (as the Docker image does) instead of `delegatron serve` |
| `DELEGATRON_ARTIFACTS_DIR` | `<install dir>/runs` | Server: where run artifacts are stored (with `delegatron serve`, or with `DELEGATRON_WORKSPACE`) |
| `DELEGATRON_CORS_ORIGINS` | localhost:3000, 8001, 8002, 8081 | Server: comma-separated browser origins allowed to call the API |
| `DELEGATRON_PROJECT_MODE` | false | Server: enable `POST /api/projects` (multi-milestone project mode) |
| `DELEGATRON_APPROVAL_TIMEOUT_MINUTES` | 30 | Server: how long an interactive run waits for plan approval before rejecting it |
| `OLLAMA_BASE_URL` | http://localhost:11434 | Server: Ollama instance the health, system-stats and defaults endpoints probe |
| `DELEGATRON_WORKSPACE_ROOT` | — | Server: local workspaces a request names must lie under it |
| `DELEGATRON_GIT_ALLOWED_HOSTS` | github.com,gitlab.com,bitbucket.org | Server: hosts a request's git URL may point at |
| `DELEGATRON_ALLOWED_BASE_URLS` | — | Server: comma-separated extra role `base_url`s an API request may use that still get the server's API keys (compared by scheme, host and port). Any other `base_url` a request sets gets no server key |
| `DELEGATRON_ALLOWED_KEY_ENVS` | — | Server: comma-separated extra `api_key_env` names an API request may select (besides the provider's default and the variable configured for the role) |
| `DELEGATRON_LOCAL_HOSTS` | — | Extra host names the zero-cloud report counts as local (e.g. a LAN model server's FQDN); also `[pipeline] local_hosts` |
| `DELEGATRON_BASELINE_TESTS` | true | Run the workspace's tests once before the first patch, so failures that already existed are never blamed on the run (`[pipeline] baseline_tests`) |
| `DELEGATRON_ALERT_WEBHOOK_URL` | — | Server: POST webhook when a run ends `error`, `exhausted`, `stalled` or `partial`, or is cancelled |
| `DELEGATRON_ELECTRICITY_RATE` | 0.12 | Electricity rate ($/kWh) for local GPU cost |
| `DELEGATRON_GPU_TDP_WATTS` | 250 | Fallback GPU power draw when nvidia-smi unavailable |
| `DELEGATRON_GPU_PRICE_USD` | 1500 | GPU purchase price for hardware amortization |
| `DELEGATRON_GPU_LIFETIME_HOURS` | 20000 | GPU expected lifetime for amortization |
| `OLLAMA_CONTEXT_LENGTH` | — | Context window assumed for an Ollama model when neither its Modelfile nor the loaded model reports one (else Ollama's 4096) |
| `GH_HOST` | github.com | `gh`'s GitHub Enterprise host; the zero-cloud report records `--create-pr` API calls to it |
| `DELEGATRON_LOG_DIR` | `<install dir>/logs` | Directory for `delegatron.log` and the server's `server.log` |
| `DELEGATRON_LOG_ROTATE` | time | Log rotation: `time` (daily) or `size` |
| `DELEGATRON_LOG_ROTATE_DAYS` | 7 | Daily logs kept with `time` rotation |
| `DELEGATRON_LOG_MAX_MB` | 100 | File size that triggers `size` rotation (5 backups kept) |
| `DELEGATRON_LOG_TIMEZONE` | utc | Log timestamps: `utc` or `local` |
| `E2B_API_KEY` | — | Required for E2B sandbox |

Precedence is CLI flags, then `delegatron.toml`, then the preset, then these
variables: a preset's own pipeline settings (the local presets set
`max_iterations`, `max_cost_usd` and `max_wall_time_minutes`) and models win
over `DELEGATRON_MAX_*` and `DELEGATRON_*_MODEL`. More server variables are in
[deployment.md](deployment.md).

### Project Config (`delegatron.toml`)

Place a `delegatron.toml` in your project root to customize behavior. The CLI
reads `<--workspace>/delegatron.toml`, else `./delegatron.toml`; `--config`
names the file explicitly. A repository cloned from a git URL is not trusted to
configure the run: its `delegatron.toml` is used only when `--config` names it.
See [`examples/delegatron.toml`](../examples/delegatron.toml) for every option.

```toml
[pipeline]
max_iterations = 10          # per task
max_cost_usd = 10.0          # 0 = no limit
sandbox = "docker"
security_mode = "warn"       # "allow", "warn" or "block"
# baseline_tests = true      # run the tests once before the first patch
# flaky_reruns = 2           # reruns of a failing test to spot flaky ones (max 5)
# tolerate_flaky_tests = false  # pass a run whose only failures are pre-existing flakes
# max_replans = 2            # re-plans before architecture issues go to the implementer
# run_git_hooks = false      # run the repository's git hooks on Delegatron's commits

[roles.architect]
provider = "anthropic"
model = "claude-sonnet-5-5"

[roles.implementer]
provider = "openai"
model = "gpt-4.1"
# temperature = 0.0          # sent with every call this role makes
# timeout_seconds = 600      # per request (local endpoints default to 900)
# prompt_version = 0         # 0 = auto: v1 for cloud endpoints, v5 for local ones

[roles.reviewer]
provider = "google"
model = "gemini-2.5-pro"

[constraints]
no_modify = ["migrations/", "package-lock.json"]   # gitignore-style patterns
forbidden_patterns = ["print(", "re:#\\s*(TODO|FIXME)"]
guidelines = ["Type hints on all functions"]
```

With `--preset`, a `[roles.<role>]` table is merged over the preset's role key
by key; changing `provider` or `base_url` drops the preset's endpoint settings
(`base_url`, keys, `prompt_version`).

A role's `temperature` (default `0.0`) is honored: it is sent with every call
that role makes. It is clamped to 0–2 (Anthropic models accept at most 1.0, so
a higher value is sent to them as 1.0), and a role's `timeout_seconds` must be
a positive number of seconds (anything else falls back to the default). The implementer's retries that would otherwise repeat the
previous attempt (a patch that changed nothing, or the same test failure twice)
use `[pipeline] retry_temperature` (default `0.4`) instead, if that is higher.

A role's API key is its `api_key`, else the variable its `api_key_env` names,
else the provider's default variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`). The default variable is sent only to that provider's own API
host. A role with `provider = "openai-compatible"` has no default key, and a
role whose `base_url` points at another host (Azure, a LiteLLM proxy) gets none
either: set `api_key_env` for a hosted OpenAI-compatible API such as Groq,
OpenRouter or xAI. Ollama and other local servers need no key. See
[Provider API Keys](security.md#provider-api-keys).

Constraint patterns are literal text; prefix an entry with `re:` for a regular
expression (an invalid one fails at load time). `forbidden_patterns` are checked
on the lines a patch adds; each `required_patterns` entry must appear in at
least one changed source file of every patch. Prose rules belong in
`guidelines`: the agents see them, but they are never pattern-checked.

Constraint violations, secrets and new dependencies are security findings. In
`security_mode = "block"`, a patch with a blocker or major finding (a secret or
a constraint violation) is not applied or committed: the implementer gets the
findings and tries again, and the task cannot be approved while one is open. In
`"warn"` (the default) findings are shown to the agents as advisories, and in
`"allow"` they are only logged. `summary.json` reports the findings still open
at the end of the run. See [Security Modes](security.md#security-modes).

### Presets

Built-in presets for common scenarios:

- **default** — Balanced quality and cost (Claude + GPT + Gemini)
- **claude-heavy** — Claude Opus architect + Claude Sonnet implementer + Gemini reviewer
- **budget** — All Gemini Flash (cheapest cloud option)
- **local** — Phi-4:14b + Qwen2.5-Coder:14b + Phi-4:14b via Ollama, v5 prompts, 5 iters per task, 1hr (free, 12GB VRAM)
- **local-oss** — Phi-4:14b + gpt-oss:20b + Phi-4:14b (best local quality; 13 GB MXFP4, fits 12 GB VRAM with some CPU offload)
- **local-oss-review** — Phi-4:14b + Qwen2.5-Coder:14b + gpt-oss:20b reviewer (strongest local review, about twice the reviewer latency)
- **local-phi4** — Phi-4:14b in all roles (single-model, fastest local, simple tasks)
- **local-heavy** — Phi-4:14b + Qwen3-Coder:30b + Phi-4:14b, 10 iters per task. **⚠️ Requires 24 GB+ VRAM.** On 12 GB cards the 30B implementer spills 30+ GB to system RAM, runs 3× slower, and exhausts the iteration budget on non-trivial tasks. Use `local` or `local-phi4` instead.
- **single-claude** — All Claude Sonnet (single-agent baseline)
- **single-gpt** — All GPT-4.1 (single-agent baseline)
- **mock** — No real API calls, for testing the pipeline

## Architecture

### Main Pipeline Flow

```
                       User Request
                            │
                   (--auto flag set?)──── yes ──► Auto-Select Preset
                            │                    (analyse keywords,
                            no                    languages, project size)
                            │◄───────────────────────────┘
                            ▼
                    Load Config / Preset
                            │
                            ▼
                ┌───────────────────────┐
                │      Architect        │──► WorkPlan
                └───────────┬───────────┘
                            │
                  (--interactive?)──── yes ──► Show plan + cost estimate
                            │                   │
                            no           [User approves?]
                            │             no         yes
                            │              │          │
                            │            STOP         │
                            │◄────────────────────────┘
                            ▼
  ┌───────── Iteration Loop (budget / iteration / time checked) ──────────┐
  │                                                                       │
  │   Context Gathering ──► Implementer ──► Sandbox ──► Reviewer ──►      │
  │   (read relevant           │           (Docker/      │         Router  │
  │    files, imports,         │            E2B)         │           │     │
  │    siblings;               │                        │     ┌─────┼───┐ │
  │    cap at 50KB)         ┌──┴──────────┐             │  approved │  │  │
  │                         │ constraints │          ┌──┴──┐  │  arch impl│
  │                         │ secrets scan│          │EMCS │  │   │    │  │
  │                         │ dep audit   │          │score│  ▼   ▼    │  │
  │                         │ git commit  │          └─────┘ DONE re-  │  │
  │                         └─────────────┘                  plan  │   │  │
  │                                                                │   │  │
  │                           ◄── fix code ────────────────────────────┘  │
  │                           ◄── re-plan  ────────────────────────┘      │
  │                                                                       │
  └───────────────────────────────────────────────────────────────────────┘
```

When `--stream` is used, every agent call (architect, implementer, reviewer,
director) emits its tokens in real time to stderr (CLI); the API server always
streams them as SSE events (API/dashboard).

### Plan-Only Flow (`delegatron plan`)

```
  Request ──► Load Config ──► Architect ──► WorkPlan (returned immediately)

  No code is generated. No sandbox. No reviewer. Useful for
  previewing how the architect would decompose your task.
```

### Refine Flow (`delegatron refine <run-id>`)

```
  Load previous run (plan, code, tests, review)
           │
           ▼
  Show current state to user
           │
           ▼
  ┌──── Prompt: "What would you like to change?" ◄──┐
  │        │                                         │
  │   "done" / Ctrl-C                     Display updated results
  │        │                                         │
  │        ▼                                         │
  │      STOP                                        │
  │                                                  │
  └──► Inject feedback as review issues              │
           │                                         │
           ▼                                         │
       Implementer ──► Sandbox ──► Reviewer          │
                                       │             │
                                       ▼             │
                                  Save checkpoint ───┘
```

### Benchmark Flow (`delegatron benchmark`)

```
  Request + Presets[A, B, C, ...]
           │
           ▼
  For each preset (sequentially):
      Load config ──► Full pipeline run ──► Record result
           │
           ▼
  Compare: status, EMCS score, iterations, cost, elapsed time
           │
           ▼
  Report: best quality / cheapest / fastest
```

### Context Gathering (Before Each Implementer Call)

```
  Current task ──► Task outputs, failing tests + the local modules they import,
                   files the review cites + their local imports,
                   files earlier tasks wrote
                       │
                       ▼
                 Test <-> source counterparts, __init__.py, files the
                 description names, sibling source files, local imports
                       │
                       ▼
                 Read files in that order (binary files skipped); a top-
                 priority file too big for what is left is excerpted
                 (head + tail), the others are left out
                 Budget: 50KB, less when the model's context window is small
                       │
                       ▼
                 {path: content} dict + repo map ──► Implementer
```

A prompt that still does not fit the model's context window is trimmed and
logged as a `context_trimmed` event.

### API Request Flow

Asynchronous run with SSE event streaming:

```
  Client                  Server                     Pipeline
    │                       │                           │
    │  POST /api/runs       │                           │
    │──────────────────────►│  Auth + Validate          │
    │  {run_id, "started"}  │                           │
    │◄──────────────────────│  create_task() ──────────►│
    │                       │                           │
    │  GET /runs/{id}/events│                           │
    │──────────────────────►│  Subscribe (queue)        │
    │                       │                           │
    │  SSE: architect_start │◄────── broadcast ────────│
    │◄──────────────────────│                           │
    │  SSE: token chunks    │◄────── broadcast ────────│
    │◄──────────────────────│                           │
    │  SSE: complete        │◄────── broadcast ────────│
    │◄──────────────────────│                           │
    │  SSE: done            │  Cleanup                  │
    │◄──────────────────────│                           │
```

Synchronous plan-only:

```
  Client                  Server
    │                       │
    │  POST /api/runs/plan  │
    │──────────────────────►│  Auth + Validate
    │                       │  Run architect (await)
    │  {plan, tasks, cost}  │
    │◄──────────────────────│
```

### Agent Communication

Agents never pass free-form text. All communication uses typed schemas:

- **WorkPlan** -- Task decomposition and acceptance criteria
- **PatchSet** -- Full file contents, search/replace edits and file deletions (or a unified diff) with rationale
- **TestReport** -- Pass/fail counts, failing test ids, output excerpts and flaky-test reruns
- **ReviewReport** -- Issues by severity with accept/reject verdict
- **ArbitrationReport** -- The director's rulings on recurring implementer/reviewer conflicts

### Evidence Hierarchy

When agents disagree, the orchestrator resolves conflicts using:

1. Failing tests (highest priority)
2. Security check results
3. Reviewer blockers
4. Director arbitration

## Advanced Features

### Interactive Mode (Plan Approval)

Use `--interactive` to review and approve the architect's plan before coding starts:

```bash
delegatron run --interactive "Refactor the auth module to use JWT"
```

In interactive mode:
1. The architect generates a plan with task decomposition and cost estimate
2. You review the plan, tasks, risk levels, and estimated cost
3. Approve to proceed with coding, or reject to stop
4. In the dashboard, the plan approval UI shows cost breakdowns per iteration and model

This is useful for expensive or high-risk changes where you want human oversight before committing compute resources.

### Local Models with Ollama

Run the entire pipeline locally with no API keys using Ollama:

```bash
# Install Ollama and pull models
ollama pull phi4:14b
ollama pull qwen2.5-coder:14b

# Run with local models
delegatron run --preset local "Add input validation"
```

The `local` preset uses the OpenAI-compatible endpoint at `http://localhost:11434/v1` with v5 prompts optimized for local models (5 iterations per task, 1hr). Local roles get a 900 s request timeout by default (`timeout_seconds` per role). Ollama needs no API key, and no provider key is sent to it. An endpoint on this machine or the local network counts as local (cost estimated from the GPU, local prompts and timeouts): loopback, private (RFC 1918, IPv6 ULA), link-local and CGNAT addresses, names ending in `.local`, `.lan`, `.internal`, `.home.arpa` or `.localhost`, and single-label names such as a Docker Compose service (`http://ollama:11434/v1`). A cloud model priced by Delegatron (Claude, GPT-4.1, Gemini) behind a local proxy such as LiteLLM is still billed at its API price. A role on another OpenAI-compatible server that does require a key needs `api_key_env` naming it. For the model's context window (`OLLAMA_CONTEXT_LENGTH`, `num_ctx`, `context_window`) see [install.md](install.md#context-window-ollama_context_length--num_ctx).

### Multi-Task Plans and Parallel Tasks

The architect may split a request into tasks with `depends_on` relations. Tasks
run one at a time in plan order, each as soon as its dependencies are finished,
and each gets its own iteration budget (`max_iterations`). A task that stalls or
runs out of iterations is skipped and the run continues with the next task;
tests it left failing are reported as pre-existing failures to later tasks
instead of blocking them.

With `parallel_tasks = true` (`[pipeline]`, or `DELEGATRON_PARALLEL_TASKS=true`)
and a git workspace, the first patches of the other ready tasks are generated
at the same time as the current task's, each in a git worktree of the run
branch. Every such patch is still applied, tested, reviewed and gated as its
own task when that task's turn comes; a patch whose files an earlier task has
changed since is regenerated. Without git, tasks run sequentially.

### Dashboard

Start the dashboard with `delegatron serve` and the Next.js frontend (see [install.md](install.md) for setup). The dashboard provides a web UI for managing runs and configuration:

- **New Run page** — Task description, preset selection, Docker image dropdown, role overrides, constraints, interactive mode toggle.
- **Code Review page** — Read-only codebase review and log file analysis from the browser. Supports git URLs.
- **Templates page** — 30 built-in templates (REST API, auth, database, security, Docker, CI, PowerShell, .NET, Terraform, Kubernetes, etc.) plus custom reusable configurations with variable substitution.
- **Projects page** — Start multi-milestone projects and follow their milestones (enable project mode in Settings > Experimental Features first). There is no benchmark page; compare presets with `delegatron benchmark` or `POST /api/benchmark`.
- **Run comparison page** — Side-by-side metrics table for any two runs with color-coded diffs.
- **Compliance page** — Zero-cloud verification with green/red verdict, event log, provider breakdown, and SHA-256 fingerprint export.
- **Runs page** — Checkbox selection, select all, bulk delete. Active run indicator in sidebar.
- **Run detail page** — Live SSE events with planning progress bar, agent reasoning, sparkline charts for Ollama VRAM and running Docker containers (with per-container CPU/memory), code viewer with tabbed files, sandbox info panel.
- **Live Thinking panel** — Real-time model reasoning stream during active runs. Extracts readable content from `<think>` blocks (local models) and JSON structured output fields. Auto-collapses when stage completes.
- **Iteration History** — Visual timeline of all iteration checkpoints on the run detail page. Click to expand per-iteration diff viewer with test/review summaries. "Restore to #N" button resets the run workspace to the commit of that iteration (downloads and file views then serve that state; the run keeps its final status, and `summary.json` records the commit to return to).
- **Local GPU cost breakdown** — For local models (Ollama), shows electricity cost, hardware amortization, and GPU time instead of $0.00. Configurable via environment variables.
- **Analytics page** — Total runs and spend, average cost per run, success rate, runs by status, preset performance table with approval/error rates.
- **Plan-only mode button** — Generate an architect plan without executing code.
- **Cost estimate display** — View estimated cost breakdowns per iteration and per model before committing to a run. Includes local GPU cost projections for Ollama presets.
- **Resume button** — Continue a run that ended without approval (error, cancelled, stalled or exhausted) from its last checkpoint, under the same run ID and in the same run workspace.
- **Provider key status** — The settings page shows which provider API keys are configured and their validation status.
- **Dark theme** — The light theme was removed.
- **System health widget** — Server, Ollama, Docker, and Disk status on the home page.
- **Toast notifications** — Success/error popups on actions.
- **Tooltips** — All interactive elements have descriptive tooltips explaining their function.

### Plugin System

Extend Delegatron with custom agents, providers, sandboxes, or lifecycle hooks. See the [Plugin Guide](plugins.md) for details.

```python
# In your package's pyproject.toml
[project.entry-points."delegatron.plugins"]
my_plugin = "my_package.plugin:MyPlugin"
```

### Security

Delegatron includes automatic secrets scanning, dependency auditing, and zero-cloud verification. See the [Security Guide](security.md) for details.

### Rate Limiting

The API server executes its runs (and benchmark runs) one at a time: a run
started while another is in progress waits for it (`queued` event). Each run
is still bounded by its own budget guardrails (`max_iterations`, `max_cost_usd`,
`max_wall_time_minutes`). The sliding-window limiter in `ratelimit.py` (10 runs/hour,
$50/hour, 3 concurrent runs) is not applied by the server.

## Usage Examples

### Example 1: Review a Repository from GitHub

```bash
# Security-focused review of a remote repo (cloned automatically)
delegatron review --workspace https://github.com/myorg/myapp.git --focus security

# Review with architect improvement suggestions
delegatron review --workspace https://github.com/myorg/myapp.git --with-suggestions
```

### Example 2: Analyze Production Logs

```bash
# Find errors and root causes in log files
delegatron review --logs /var/log/myapp/error.log --focus bugs

# Analyze multiple log files together
delegatron review --logs app.log --logs nginx.log --focus performance
```

### Example 3: Windows / .NET Project

```bash
# Use a .NET Docker image for C# projects
export DELEGATRON_DOCKER_IMAGE=mcr.microsoft.com/dotnet/sdk:9.0
delegatron run "Add a health check endpoint to the ASP.NET API with unit tests"

# Or use a built-in template (it sets the .NET SDK image itself)
delegatron template run dotnet-api --var domain="order tracking" --var database=PostgreSQL

# PowerShell module
export DELEGATRON_DOCKER_IMAGE=mcr.microsoft.com/powershell:lts
delegatron run "Create a PowerShell module for managing Azure resources"
```

### Example 4: Local-Only Pipeline (Zero Cloud)

```bash
# Fully local — no data leaves your machine
delegatron run --preset local "Add input validation to the API"

# Verify zero-cloud compliance: "zero_cloud" in runs/<run-id>/summary.json,
# full report in runs/<run-id>/zero_cloud_report.json (listed by `show`)
delegatron show <run-id>

# Or via the API
curl http://localhost:8000/api/runs/<run-id>/compliance
```

### Example 5: Interactive Plan Approval

```bash
# Review the plan before any code is generated
delegatron run --interactive "Refactor the auth module to use JWT tokens"

# The architect generates a plan, you review cost + tasks, then approve or reject
```

### Example 6: Multi-Repo Workflow

```bash
# Review one repo, then run changes in another
delegatron review --workspace /path/to/frontend
delegatron run --workspace /path/to/backend "Add CORS headers matching the frontend origins"
```

## Troubleshooting

### Missing API key or provider SDK

A role whose provider SDK or API key is missing ends the run `error`: an
`ImportError` when the SDK is not installed, an authentication error from the
first call (for the architect, `Architect failed: ...`) when the key is not
set. Install the `cloud-providers` extra for `anthropic` and `google` roles,
set the key of every provider your preset uses (see
[Set API Keys](#set-api-keys)), or use `--preset mock` for testing.

### Tests failing in sandbox

The Docker sandbox needs the `docker` CLI on PATH (without it the run ends
`error` before the architect runs) and a running daemon (`docker info`).
Otherwise configure E2B with `E2B_API_KEY` (`--sandbox e2b`), or use
`--sandbox local` (no isolation).

### Pipeline stalls

The orchestrator stops working on a task that makes no progress:

- `no_progress` — two attempts in a row changed no file;
- `recurring_failure` — the same tests failed the same way on the last two attempts;
- `recurring_issues` — with no failing test, the reviewer raised mostly the same issues twice.

Only the no-change rule applies right away; the others wait until the task has
had 3 attempts (5 with a local implementer). Before stopping, a recurring
conflict between implementer and reviewer goes to the director once per task,
whose ruling gives the implementer one more directed attempt
(`enable_arbitration`, on by default); a ruling that the plan itself must change
(`fix_target: "architecture"`) asks the architect to re-plan instead, at most
`max_replans` times per run. A stalled task is skipped and the run
continues with the next task; the run ends `stalled` only when no task is left.
The `stall_detected` event and the task's entry in `summary.json`
(`task_outcomes`) give the reason. Increase `--max-iterations` if your tasks need
more cycles.

### Cost overruns

Set `--max-cost`, `[pipeline] max_cost_usd` or `DELEGATRON_MAX_COST_USD` (which a preset's or `delegatron.toml`'s own setting overrides) to a comfortable limit. Use `--preset budget` for cost-sensitive work. The pipeline halts when the budget is reached.

### Local runs produce identical wrong output every time ("template collapse")

If every run with a local preset produces the same hallucinated output —
regardless of your task — Ollama is in a stuck state. Its KV cache or
connection pool has latched onto a prior template and won't let go. This
is a known failure mode we've hit repeatedly during local model work.

**Fix:** restart Ollama.

- **From the dashboard:** Settings → Ollama → "Restart Ollama" button
  (unloads all models and cycles the process)
- **From the CLI (POSIX):** `pkill -f "ollama serve"; ollama serve &`
- **From the CLI (Windows):** `taskkill /IM ollama.exe /F; ollama serve`
- **Via API:** `POST /api/ollama/restart` (platform-aware — unloads models
  then kills/restarts the Ollama process on Linux/macOS/Windows)

After restart, the next run will be slow (models reload from disk into
VRAM), then normal speed resumes. If the collapse returns within a few
runs, check for GPU memory pressure with `nvidia-smi` — multi-model
presets like `local-heavy` can push a 12GB card to its limit.
