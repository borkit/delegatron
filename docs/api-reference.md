# Delegatron API Reference

## REST API

The Delegatron server exposes a REST API for triggering and monitoring runs programmatically.

### Starting the Server

```bash
delegatron serve --host 0.0.0.0 --port 8000 --workspace /path/to/repos
```

`--host` defaults to `0.0.0.0`, `--port` to `8000` and `--workspace` (the default workspace of runs and reviews) to the current directory; a workspace that is the Delegatron installation itself is replaced by `~/delegatron-workspace`. Serving the app directly (`uvicorn delegatron.server.app:app`, as the Docker image does) takes the workspace from `DELEGATRON_WORKSPACE`. Run artifacts go to `DELEGATRON_ARTIFACTS_DIR` when set, else the installation's `runs/`. When the dashboard is built (`dashboard/.next`), `delegatron serve` also starts it (`next start`) on the next port (`8001` by default). Interactive OpenAPI docs are served at `/docs`.

The server runs one pipeline at a time: a run started while another is in progress waits (its event stream gets a `queued` event) until the slot is free. Run it as a single process (`uvicorn --workers 1`); active runs, streams and pending plan approvals are kept in memory. Browsers may call the API from the origins in `DELEGATRON_CORS_ORIGINS` (comma-separated, default `http://localhost:3000,http://localhost:8001,http://localhost:8002,http://localhost:8081`).

### Authentication

Authentication is off unless `DELEGATRON_AUTH_ENABLED` is `true` (or `1`/`yes`); without it every endpoint is open and the server logs an "INSECURE DEV MODE" warning. With it on, requests need an API key in the `Authorization` header:

```
Authorization: Bearer dlg_your_api_key_here
```

and the server refuses to start unless `DELEGATRON_STREAM_SECRET` is set (it signs the short-lived URLs for SSE and downloads, see `GET /api/stream-token`). Runs, projects, benchmarks and SWE-bench evaluations are then scoped to the key's team (HTTP `403` for another team's run); changing server settings and restarting Ollama need the `admin` team role. `GET /api/health`, `GET /api/health/full`, `GET /api/defaults`, `GET /api/system/stats` and `GET /api/queue` never need a key, and `POST /api/webhooks/github` is checked by its signature instead. Missing or invalid keys get HTTP `401`.

There are no REST endpoints for teams or API keys. Keys and teams live in the server's SQLite database (`<DELEGATRON_DATA_DIR>/delegatron.db`, default `data/delegatron.db`) and are created in Python; the raw key is shown only once (only its SHA-256 hash is stored):

```python
from delegatron.server.auth import APIKeyManager
from delegatron.server.db import init_db
from delegatron.server.teams import TeamStore

db = "data/delegatron.db"
init_db(db)
team = TeamStore(db).create_team("my-team", owner_id="alice", owner_email="alice@example.com")
raw_key, record = APIKeyManager(db).create_key(user_id="alice", team_id=team.team_id)
print(raw_key)  # dlg_...
```

The team owner is the team's `admin`; `TeamStore.add_member` adds `member` or `viewer` users, and `APIKeyManager.revoke_key` revokes a key.

### Endpoints

#### `POST /api/runs`

Start a new pipeline run.

**Request:**
```json
{
  "request": "Add a /health endpoint returning {status: ok}",
  "preset": "default",
  "workspace": "/path/to/repo",
  "branch": null,
  "config_path": null,
  "sandbox": "docker",
  "docker_image": null,
  "max_iterations": 5,
  "max_cost_usd": 5.0,
  "max_wall_time_minutes": 60,
  "interactive": false,
  "create_pr": false,
  "pr_base": null,
  "pr_draft": false,
  "architect": {
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "base_url": null,
    "api_key_env": null,
    "max_tokens": 4096,
    "temperature": 0.3
  },
  "implementer": {
    "provider": "openai",
    "model": "gpt-4.1",
    "base_url": null,
    "api_key_env": null,
    "max_tokens": 8192,
    "temperature": 0.2
  },
  "reviewer": {
    "provider": "google",
    "model": "gemini-2.5-pro",
    "base_url": null,
    "api_key_env": null,
    "max_tokens": 4096,
    "temperature": 0.1
  },
  "constraints": {
    "no_modify": ["README.md", "migrations/"],
    "required_patterns": ["def test_"],
    "forbidden_patterns": ["import pdb", "re:\\beval\\("],
    "guidelines": ["Validate all request input with Pydantic models"]
  }
}
```

All fields except `request` (1 to 100,000 characters) are optional; omitted ones come from the preset, the config file and the `DELEGATRON_*` environment.

- **`preset`** is a preset name (`delegatron list-presets`) or `"auto"`, which picks one from the request's complexity as `delegatron run --auto` does. Unknown presets are HTTP `422`.
- **`sandbox`** is `docker` or `e2b`; **`docker_image`** replaces the sandbox image for this run. `max_iterations` (1 to 100), `max_cost_usd` (0 to 1000) and `max_wall_time_minutes` (1 to 1440) replace the preset's limits.
- **`interactive`** makes the run wait for `POST /api/runs/{run_id}/approve` or `/reject` after planning (the event stream sends `plan_pending_approval`); a plan nobody answers within `DELEGATRON_APPROVAL_TIMEOUT_MINUTES` (default 30) is rejected.
- **`create_pr`**, **`pr_base`** and **`pr_draft`** open a GitHub pull request when the run ends `approved`, as the CLI's `--create-pr` does (needs an authenticated `gh` CLI; a failed PR never fails the run and is recorded under `pull_request` in `summary.json`).
- **`workspace`** is an existing local directory or a git URL. Local paths must lie under `DELEGATRON_WORKSPACE_ROOT` when that is set. Git URLs must be `https://host/path`, `ssh://[user@]host/path` or `user@host:path` (a bare `github.com/org/repo` is cloned over https) on a host listed in `DELEGATRON_GIT_ALLOWED_HOSTS` (comma-separated, default `github.com,gitlab.com,bitbucket.org`). Values starting with `-`, `ext::`/`file:` transports and plain `http://` URLs are rejected. Each run works in its own copy, `<workspace>/.delegatron-runs/<run_id>`: a clone (same history and `origin`, so `create_pr` can open a pull request) when the workspace is a git repository root, otherwise a fresh repository.
- **`branch`** is the branch to clone when `workspace` is a git URL; by default the remote's default branch is used. It must not start with `-`.
- **`architect`, `implementer`, `reviewer`** override the preset per role. A `base_url` must be an `http://` or `https://` URL. When an override points a role at a `base_url` the operator has not configured (a role `base_url` of the preset or TOML) or allowed in `DELEGATRON_ALLOWED_BASE_URLS` (comma-separated URLs, compared by scheme, host and port), the server sends that endpoint no API key of its own (nor an `ANTHROPIC_AUTH_TOKEN` from its environment). A role moved to another provider or `base_url` gets its prompt version chosen again for that endpoint (v1 for cloud, v5 for local). `api_key_env` may only name the provider's default key variable, the variable the operator configured for the role, or one listed in `DELEGATRON_ALLOWED_KEY_ENVS`. A role's `temperature` (0.0 to 2.0, default `0.0`) is honored: it is sent with every call that role makes (Anthropic models get at most 1.0). An override may also set `prompt_version` (0 or more; `0` resolves it by endpoint) and `timeout_seconds` (1 to 7200, per provider request); both are kept as given. A role moved to another endpoint without them gets that endpoint's prompt version and default timeout, and drops the old endpoint's `base_url`, keys, prompt version and timeout.
- **`constraints`** are enforced on every patch. `required_patterns` and `forbidden_patterns` may be a list or a `{name: pattern}` table, as in `delegatron.toml`. Pattern entries are literal text unless prefixed with `re:` (a regular expression). `no_modify` entries ending in `/` protect a directory; entries without `/` also match the file name anywhere (`.env` protects `config/.env`). `guidelines` are prose rules shown to the agents and never pattern-checked. They are added to any `[constraints]` from the run's config file.

- **`config_path`** names an existing TOML file under the server's workspace root to use as the run's config, as a relative path without `..`. It may not lie in a Delegatron directory (`.delegatron-runs/`, the run artifacts): a file a run wrote is never operator config. Without `config_path`, a `delegatron.toml` in the server process's working directory is used when one exists; the server does not look for one in the requested workspace.

The run branch forks from the branch the run workspace has checked out (for a git URL, `branch`, else the remote's default branch).

Returns HTTP `422` for an unknown preset or an invalid workspace, branch name, `config_path`, role override (the detail names the field, e.g. `architect.api_key_env`) or constraint pattern; no run is created then. A git URL is cloned after the run has started: a clone that fails ends the run with status `error` (unlike `POST /api/runs/plan` and `POST /api/review`, which answer HTTP `502`).

**Response:** the new run's id (12 hex characters).
```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/a1b2c3d4e5f6/events"
}
```

#### `POST /api/runs/plan`

Plan-only mode. Runs only the architect step and returns the plan synchronously. No code is generated, no sandbox is launched, and no background task is created. This is the API equivalent of `delegatron plan`.

**Request:**

Same body as `POST /api/runs` (see `RunRequest` above). Only `request`, `preset` (not `"auto"`), `workspace`, `branch` and the role overrides are used; the architect plans against the workspace itself (no run copy is made) and the other fields are ignored.

**Response:** `tasks` and `assumptions` repeat those of the full `plan` (a `WorkPlan`).
```json
{
  "run_id": "plan-abc123def456",
  "status": "planned",
  "plan_summary": "Add a /health endpoint to the FastAPI app...",
  "tasks": [
    {
      "task_id": "task-1",
      "title": "Health endpoint",
      "description": "Create health endpoint in app.py",
      "outputs": ["app.py", "tests/test_health.py"],
      "risk_level": "low",
      "depends_on": []
    }
  ],
  "assumptions": [
    "The project uses FastAPI",
    "No authentication required for /health"
  ],
  "cost_usd": 0.012,
  "plan": {
    "summary": "...",
    "assumptions": [...],
    "open_questions": [...],
    "tasks": [...],
    "acceptance": { "criteria": [...], "tests": [...] },
    "termination": { "done_when": [...], "stop_limits": {...} }
  }
}
```

Returns HTTP `422` if the preset is unknown, the request fails the same validation as `POST /api/runs` or the architect produces no usable plan, HTTP `502` when cloning a git URL fails (the detail is git's message, with any credentials in the URL removed), and HTTP `500` if the architect call raises.

#### `POST /api/estimate`

Estimate the cost range for a pipeline run based on model pricing. Returns min/max estimates assuming typical token usage per iteration.

**Request:**
```json
{
  "preset": "default",
  "architect": {
    "model": "claude-sonnet-5-5"
  },
  "implementer": {
    "model": "gpt-4.1"
  },
  "reviewer": {
    "model": "gemini-2.5-pro"
  },
  "max_iterations": 5
}
```

All fields are optional. `preset` defaults to `"default"` if omitted. Per-role overrides accept the same `RoleOverride` shape as `POST /api/runs` and are applied to the preset as for a run. `max_iterations` defaults to `5`.

Whether a role is local is decided from its endpoint (`provider` and `base_url`), never the model name, as the run's cost tracker and `delegatron estimate` do. A local role has no API price: its cost is the estimated GPU electricity and hardware amortization, listed under `breakdown.local_gpu`. Each role in `breakdown` carries `is_local`, and `breakdown.local_gpu.roles` names the local roles; `local_gpu` also gives `min_local_usd`/`max_local_usd`, the electricity and hardware parts (`min_electricity_usd`, `max_hardware_usd`, ...), `min_gpu_seconds`/`max_gpu_seconds` (at an assumed 30 output tokens per second), `gpu_watts` and `electricity_rate_kwh`. When no role has an API cost, `min_cost_usd`/`max_cost_usd` are the local estimates.

**Response:**
```json
{
  "min_cost_usd": 0.206,
  "max_cost_usd": 0.8177,
  "breakdown": {
    "architect": {
      "model": "claude-sonnet-5-5",
      "calls": 1,
      "is_local": false,
      "min_cost_usd": 0.021,
      "max_cost_usd": 0.084
    },
    "implementer": {
      "model": "gpt-4.1",
      "calls": 5,
      "is_local": false,
      "min_cost_usd": 0.11,
      "max_cost_usd": 0.44
    },
    "reviewer": {
      "model": "gemini-2.5-pro",
      "calls": 5,
      "is_local": false,
      "min_cost_usd": 0.075,
      "max_cost_usd": 0.29375
    }
  },
  "models": {
    "architect": "claude-sonnet-5-5",
    "implementer": "gpt-4.1",
    "reviewer": "gemini-2.5-pro"
  }
}
```

Returns HTTP `422` if the preset is unknown or a role override is not allowed.

#### `POST /api/review`

Review an existing codebase or log files (read-only). No code changes are made.

**Request:**
```json
{
  "workspace": "/path/to/repo",
  "branch": null,
  "preset": "default",
  "focus": "security",
  "question": "Are there any SQL injection vulnerabilities?",
  "with_suggestions": false,
  "log_content": null,
  "architect": null,
  "reviewer": null
}
```

All fields are optional. `workspace` defaults to the server's configured workspace; it may be a directory that is not a git checkout. `workspace`, `branch` and the `architect`/`reviewer` overrides follow the same rules as `POST /api/runs` (HTTP `422` otherwise; HTTP `502` when cloning a git URL fails, with git's message as the detail; HTTP `404` when the workspace does not exist). `preset` defaults to `"local"`. `focus` can be `"security"`, `"bugs"`, `"performance"`, or `"all"` (the same focus texts as `delegatron review`). Provide `log_content` (raw text; only its last 2,000 lines are sent) for log analysis mode instead of source code review. `with_suggestions` also runs the architect on the findings and returns its improvement plan as `suggestions`, as `delegatron review --with-suggestions` does.

The reviewer gets the repository map and the most significant source files, as `delegatron review` builds them: at most half the reviewer's context window when it is known (`context_window`), passed as files the agent can trim to fit its window.

**Response:** `accepted` is the reviewer's `acceptance_met`; `suggestions` is `null` unless `with_suggestions` was set. Issue severities are `blocker`, `major`, `minor` or `nit`.
```json
{
  "review_id": "review-1a2b3c4d",
  "summary": "Found 3 security issues and 2 code quality improvements...",
  "accepted": false,
  "issues": [
    {
      "severity": "major",
      "title": "SQL injection in user query",
      "details": "...",
      "recommended_fix": "Use parameterized queries",
      "evidence": "app.py:42"
    }
  ],
  "files_reviewed": 15,
  "cost_usd": 0.0,
  "project": {
    "path": "/path/to/repo",
    "languages": ["python", "javascript"],
    "frameworks": ["fastapi"]
  },
  "suggestions": null
}
```

The review is saved to `<server workspace>/reviews/<review_id>/summary.json`.

#### `GET /api/reviews`

List all past code reviews with summary info, in reverse order of their ids (not by date). With authentication on, the list is empty: saved reviews do not record a team.

**Response:**
```json
[
  {
    "review_id": "review-1a2b3c4d",
    "summary": "Found 3 issues...",
    "accepted": false,
    "issue_count": 3,
    "files_reviewed": 15,
    "path": "/path/to/repo",
    "languages": ["python"]
  }
]
```

#### `GET /api/defaults`

Return server defaults from the environment for the dashboard to pre-populate forms: `DELEGATRON_SANDBOX` (default `docker`), `DELEGATRON_MAX_ITERATIONS` (`5`), `DELEGATRON_MAX_COST_USD` (`5.0`), `DELEGATRON_MAX_WALL_TIME` (`60`), the current log settings, the server workspace, and the `DELEGATRON_PARALLEL_TASKS` / `DELEGATRON_PROJECT_MODE` flags. Ollama is probed at `OLLAMA_BASE_URL` (default `http://localhost:11434`).

**Response:**
```json
{
  "sandbox": "docker",
  "max_iterations": 5,
  "max_cost_usd": 5.0,
  "max_wall_time_minutes": 60,
  "log_level": "INFO",
  "log_timezone": "utc",
  "workspace": "/path/to/repos",
  "auth_enabled": false,
  "provider_keys": {
    "anthropic": true,
    "openai": true,
    "gemini": false,
    "e2b": false,
    "ollama": true
  },
  "ollama": {
    "running": true,
    "models": ["phi4:14b", "qwen2.5-coder:14b"],
    "url": "http://localhost:11434"
  },
  "parallel_tasks": false,
  "project_mode": false
}
```

#### `GET /api/provider-keys`

Check which LLM provider API keys are configured on the server. Returns a boolean for each key indicating whether it is set (the actual key values are never exposed).

**Response:**
```json
{
  "ANTHROPIC_API_KEY": true,
  "OPENAI_API_KEY": true,
  "GEMINI_API_KEY": false
}
```

#### `GET /api/runs`

List all runs (of the caller's team when authentication is on). There are no query parameters: the response is an array with one entry per run directory, each the run's `summary.json` plus `run_id`, `request`, `created_at` and the run's `_preset` and `_team_id` from its `meta.json`. A run that has not written a summary yet, or a resumed run whose new session is still going, is listed with status `"running"`.

**Response** (summary fields abridged):
```json
[
  {
    "run_id": "a1b2c3d4e5f6",
    "status": "approved",
    "iterations": 3,
    "total_cost_usd": 0.42,
    "elapsed_seconds": 312.4,
    "request": "Add a /health endpoint...",
    "plan_summary": "Add a /health endpoint to the FastAPI app...",
    "is_approved": true,
    "error": "",
    "created_at": "2026-04-03T12:00:00+00:00",
    "_preset": "default",
    "_team_id": "default"
  }
]
```

#### `GET /api/runs/{run_id}`

Get details of a specific run: its `summary.json`, plus `files_created`, `test_report` (the latest test report's `passed`, `total`, `failures` and the last 500 characters of `stderr`), `review_details` (the latest review's `summary` and `issues`) and `_meta` (the preset, sandbox, Docker image, `max_iterations`, `max_cost_usd` and role overrides from `meta.json`, for the retry dialog). `files_created` lists every file the run created or modified across all iterations and tasks (files a sandbox left behind, such as bytecode or installed packages, are not the run's). The run's `summary.json` fields (`termination_reason`, `files_deleted`, `task_outcomes`, `skipped_tasks`, `regressed_tests`, `fixed_baseline_failures`, `flaky_tests`, `security`, `roles`, `cost_breakdown`, `zero_cloud`, `pull_request`, ...) are described in the [user guide](user-guide.md#run-results-runsrun-id). An active run without a summary yet returns only `run_id`, `status: "running"` and `request`; HTTP `404` if the run does not exist.

A finished run's `status` is one of `approved`, `partial` (some tasks approved, others given up, or an accepted test failing or gone at the end), `exhausted` (a budget ran out, also after some tasks were approved), `stalled`, `error` or `cancelled`. A resumed run reads `running` until its new session ends.

**Response** (summary fields abridged):
```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "approved",
  "iterations": 3,
  "total_cost_usd": 0.42,
  "elapsed_seconds": 312.4,
  "request": "Add a /health endpoint...",
  "plan_summary": "Add a /health endpoint to the FastAPI app...",
  "is_approved": true,
  "termination_reason": "...",
  "branch": "delegatron/run-a1b2c3d4e5f6",
  "files_changed": ["src/app.py", "tests/test_app.py"],
  "files_created": ["src/app.py", "tests/test_app.py"],
  "test_report": { "passed": true, "total": 3, "failures": 0, "stderr": "" },
  "review_details": { "summary": "Code looks good, all tests pass", "issues": [] },
  "_meta": {
    "preset": "default",
    "sandbox": "docker",
    "docker_image": null,
    "max_iterations": 5,
    "max_cost_usd": 5.0,
    "architect": null,
    "implementer": null,
    "reviewer": null
  }
}
```

#### `DELETE /api/runs/{run_id}`

Delete a run, its artifacts and its run workspace (`<workspace>/.delegatron-runs/<run_id>`). A run workspace is kept until the run is deleted whenever the run has a checkpoint (even only the plan's), so it can be resumed or restored; a cancelled or failed run without one loses its workspace right away. A workspace that cannot be removed is logged; the response is still `deleted`. A running run is cancelled first (HTTP `409` if it has not stopped within 10 seconds). Returns HTTP `404` if the run does not exist and HTTP `400` for a malformed `run_id`.

**Response:**
```json
{
  "status": "deleted",
  "run_id": "a1b2c3d4e5f6"
}
```

#### `POST /api/runs/{run_id}/retry`

Start a **new** run (new `run_id`, fresh run workspace) from a finished run's request and the settings saved in its `meta.json` (preset, workspace, branch, sandbox, Docker image, limits, PR options, constraints, role overrides). Unless `fresh_start` is set, the issues of the run's latest review are appended to the request ("Fix the following issues from the previous review: ..."), and `strategy` is `"refine"`; otherwise, or when there are no review issues, the original request is used and `strategy` is `"rerun"`.

**Request:** The body is optional. `preset` and the role overrides replace the saved ones.

```json
{
  "fresh_start": false,
  "preset": "local",
  "architect": null,
  "implementer": { "model": "qwen3-coder:30b" },
  "reviewer": null
}
```

**Response:**
```json
{
  "run_id": "f6e5d4c3b2a1",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/f6e5d4c3b2a1/events",
  "strategy": "refine"
}
```

Returns HTTP `404` if the run has no `summary.json`, HTTP `400` if no request text can be found for it, and HTTP `422` if the saved settings are no longer valid.

#### `POST /api/runs/{run_id}/rerun`

Start a new run with the same request and configuration: `POST /api/runs/{run_id}/retry` with `fresh_start: true`, without `strategy` in the response.

**Response:**
```json
{
  "run_id": "f6e5d4c3b2a1",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/f6e5d4c3b2a1/events"
}
```

#### `POST /api/runs/{run_id}/resume`

Continue a run from its latest checkpoint (`state_NNN.json`), as `delegatron resume` does. The run keeps its `run_id` and its run workspace (`<workspace>/.delegatron-runs/<run_id>`, which holds the code of the earlier iterations), and uses the configuration saved in its `meta.json` (preset, workspace, sandbox, role overrides, constraints). The plan, the completed tasks, the current task and the iteration count are restored from the checkpoint, so the architect does not plan again and finished tasks are not redone.

Runs with status `"error"`, `"cancelled"`, `"stalled"` or `"exhausted"` can be resumed. Each resume is recorded in the run's `meta.json`, in a `resumes` list (`resumed_at`, `from_iteration`, `from_status`, `overrides`), and as a `run_resumed` event; the original settings are kept. While the new session runs, the run is listed as `"running"`. Its elapsed wall time continues from the checkpoint, and each session is billed only for spend not billed before.

**Request:** The body is optional. It raises the run's limits for the continued run; without it, an `exhausted` run would stop again at the same limit. Omitted fields keep the values from `meta.json`.

```json
{
  "max_iterations": 20,
  "max_wall_time_minutes": 90,
  "max_cost_usd": 10.0
}
```

**Response:** the original `run_id`.
```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/a1b2c3d4e5f6/events"
}
```

A run restored to an earlier iteration (`POST /api/runs/{run_id}/restore`) continues from that iteration's checkpoint.

Errors (a fresh run is never started instead):

- `404`: the run is not found.
- `400`: the run's status cannot be resumed, for example `"approved"`.
- `409`: the run has no checkpoint (use `POST /api/runs/{run_id}/retry` to start it again), its run workspace no longer exists, or the run is still active.
- `422`: the body is invalid, or a saved role override is no longer allowed (see the role override rules under `POST /api/runs`).

#### `GET /api/runs/{run_id}/plan`

Get the pending plan for interactive approval.

**Response:**
```json
{
  "status": "pending",
  "plan": { "summary": "...", "tasks": [...], "cost_estimate": {...} }
}
```

Returns `{"status": "no_pending_plan"}` when no plan is awaiting approval. A plan waits at most `DELEGATRON_APPROVAL_TIMEOUT_MINUTES` (default 30) and is then rejected.

#### `POST /api/runs/{run_id}/approve`

Approve a pending plan in interactive mode. HTTP `404` when no plan is pending.

**Response:**
```json
{
  "status": "approved"
}
```

#### `POST /api/runs/{run_id}/reject`

Reject a pending plan in interactive mode; the run ends with status `error` ("Plan rejected by user") before any code is written. HTTP `404` when no plan is pending.

**Response:**
```json
{
  "status": "rejected"
}
```

#### `GET /api/runs/{run_id}/files`

Get the contents of every file the run created or modified, across all iterations and tasks. Files are read from the run workspace while it exists (so they reflect a restore); otherwise they are rebuilt from the saved patch sets.

**Response:**
```json
{
  "files": {
    "src/app.py": "from fastapi import FastAPI\n...",
    "tests/test_app.py": "import pytest\n..."
  }
}
```

Returns an empty `files` dict if the run produced no files, and HTTP `404` if the run does not exist.

#### `GET /api/runs/{run_id}/iterations`

List all iteration checkpoints (`state_NNN.json`) for a run with per-iteration metadata. Use this to browse the progression of a run across iterations. HTTP `404` if the run does not exist.

**Response:**
```json
{
  "run_id": "a1b2c3d4e5f6",
  "iterations": [
    {
      "iteration": 1,
      "status": "running",
      "is_approved": false,
      "patch_applied": true,
      "total_cost_usd": 0.012,
      "files_changed": ["src/app.py", "tests/test_app.py"],
      "rationale": "Added /health endpoint with uptime tracking",
      "tests": { "passed": false, "total": 3, "failures": 1 },
      "review": {
        "reviewer_accepted": false,
        "summary": "Missing error handling for uptime counter",
        "issue_counts": { "major": 1, "minor": 1 }
      }
    },
    {
      "iteration": 2,
      "status": "approved",
      "is_approved": true,
      "patch_applied": true,
      "total_cost_usd": 0.024,
      "files_changed": ["src/app.py"],
      "rationale": "Fixed error handling, added try/except",
      "tests": { "passed": true, "total": 3, "failures": 0 },
      "review": {
        "reviewer_accepted": true,
        "summary": "Code looks good, all tests pass",
        "issue_counts": {}
      }
    }
  ]
}
```

#### `GET /api/runs/{run_id}/iterations/{iteration}/diff`

Get the diff and full artifact details for a specific iteration: its patch set's `diff_unified`, `rationale` and `files` (rebuilt from the diff when the patch set has none), plus the iteration's full saved `test_report` and `review_report` when they exist.

**Response** (reports abridged):
```json
{
  "run_id": "a1b2c3d4e5f6",
  "iteration": 1,
  "diff_unified": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,8 @@\n...",
  "rationale": "Added /health endpoint with uptime tracking",
  "files": {
    "src/app.py": "from fastapi import FastAPI\n..."
  },
  "test_report": { "passed": false, "total": 3, "failures": 1, "stderr": "..." },
  "review_report": { "summary": "...", "issues": [...], "acceptance_met": false }
}
```

Returns HTTP `404` if the iteration or patch does not exist.

#### `POST /api/runs/{run_id}/restore`

Reset a finished run's workspace to the commit of an earlier iteration (`git reset --hard`), so downloads and the file view serve that iteration's files. The run keeps its final status; `summary.json` records `restored_to_iteration`, `restored_sha` and `pre_restore_sha` (the run's final HEAD, to restore back to). Restoring is repeatable and idempotent.

**Request:**
```json
{
  "iteration": 1
}
```

**Response:**
```json
{
  "status": "restored",
  "run_id": "a1b2c3d4e5f6",
  "restored_to_iteration": 1,
  "restored_sha": "3f2c9a1..."
}
```

`iteration` must be an integer of at least 1 (HTTP `422` otherwise). Returns HTTP `404` if the run or checkpoint does not exist. Returns HTTP `409` if the run is currently active, has no git workspace to restore, or the reset fails. The restore is recorded as an `iteration_restored` event, and a later `POST /api/runs/{run_id}/resume` continues from the restored iteration.

#### `GET /api/runs/{run_id}/compliance`

Export the zero-cloud report (`zero_cloud_report.json`) of a finished run. It lists every network destination the orchestrator used for the run (LLM endpoints, sandbox, artifact store, PR push, failure-alert webhook) and flags each non-local one as a violation. It is an audit of the configured destinations, not traffic monitoring or enforcement (`summary.enforcement_mode` is `"audit"`). The `fingerprint` is a SHA-256 checksum over the rest of the report: recomputing it spots accidental edits, but it is not a signature.

**Response:**
```json
{
  "run_id": "a1b2c3d4e5f6",
  "cloud_free": true,
  "timestamp": "2026-04-09T12:00:00+00:00",
  "total_events": 1,
  "violations": [],
  "summary": {
    "total_events": 1,
    "local_events": 1,
    "cloud_events": 0,
    "by_category": { "llm_call": 1 },
    "by_provider": { "openai": 1 },
    "enforcement_mode": "audit"
  },
  "events": [
    {
      "timestamp": "2026-04-09T12:00:00+00:00",
      "category": "llm_call",
      "role": "implementer",
      "provider": "openai",
      "host": "localhost",
      "is_local": true,
      "details": { "model": "qwen3-coder:30b", "base_url": "http://localhost:11434/v1" }
    }
  ],
  "fingerprint": "9f2c..."
}
```

A violation carries `timestamp`, `category`, `role`, `provider`, `host` and a `reason`. When only the summary's `zero_cloud` entry exists, the response is that entry (`verified`, `violations` as a count, `fingerprint`) with `run_id` and `"detail": "full report not available"`. Returns HTTP `404` if the run or compliance report is not found.

#### `GET /api/runs/{run_id}/download`

Download the code a completed run produced as a zip file: every file it created or modified across all iterations and tasks (as `GET /api/runs/{run_id}/files` returns them), plus a generated `DELEGATRON_RUN.md`.

**Response:** Streaming zip file attachment (`application/zip`).

Filename format: `delegatron-{short_id}-{task_slug}.zip` (the first 8 characters of the run id and the first 40 characters of the request, other characters than letters, digits, `-` and `_` replaced by `_`). Returns HTTP `404` if the run has no `summary.json` or produced no files. With authentication on, a browser download uses a signed URL from `GET /api/stream-token`.

#### `GET /api/runs/{run_id}/events`

Stream pipeline events via Server-Sent Events (SSE).

```
GET /api/runs/{run_id}/events
Accept: text/event-stream
```

Each message is `data: <JSON>`; the JSON's `event` field names it. On connect the server first replays the run's `events.jsonl`, then sends live events, and ends the stream with `event: done`. For a run that is no longer active the stream is the replay alone (its `done` carries `{"source": "replay"}`); HTTP `404` if the run is not active and has no `events.jsonl`. With authentication on, `EventSource` cannot send the `Authorization` header: use a signed URL from `GET /api/stream-token`.

**Live events:**
- `queued` — the run waits for the server's run slot (another run is in progress)
- `started`, `pipeline_executing` — the run got the slot; the pipeline starts
- `plan_pending_approval` — interactive mode: `plan` awaits `POST /approve` or `/reject`
- `architect_start`, `implementer_start`, `reviewer_start`, `director_start` — an agent call starts
- `token` — a chunk of streamed model output (`text`, `role`); never written to `events.jsonl`
- `architect_complete` (`summary`, `task_count`), `implementer_pass`, `implementer_complete`, `tests_complete` (`passed`), `reviewer_complete` (`reviewer_accepted`, `accepted`, `emcs_override`)
- `parallel_start`, `parallel_complete` — independent tasks' first patches generated concurrently (`parallel_tasks`)
- `task_advanced`, `task_skipped` — a task was approved or given up and the next one starts
- `context_trimmed`, `plan_normalized` — a prompt was trimmed to fit its context window; the plan was repaired
- `complete` — the pipeline returned; `state` is the final run state
- `error` — the run failed (`error`), or `"Run cancelled"`

The pipeline's other events (`pipeline_start`, `workspace_commit`, `emcs_score`, `plan_rejected`, `pipeline_complete`, ...) are written to `events.jsonl` only, so a client sees them in the replay when it connects (or reconnects). `progress`, `sandbox_starting` and `sandbox_complete` are kept only in the run state's `messages`, which the final `complete` event carries.

#### `GET /api/analytics`

Usage analytics over all runs (of the caller's team when authentication is on). There are no query parameters.

**Response:**
```json
{
  "total_runs": 42,
  "total_cost_usd": 18.5,
  "avg_cost_per_run": 0.4405,
  "avg_iterations": 2.8,
  "success_rate": 0.7143,
  "most_used_preset": "local",
  "runs_by_status": { "approved": 30, "partial": 4, "exhausted": 5, "error": 3 }
}
```

`success_rate` is the share of runs with `is_approved`. A `cost_by_model` object is added only when run summaries carry per-model costs under `cost_breakdown`; current summaries break costs down by role (`by_role`), so it is normally absent.

#### `POST /api/runs/{run_id}/cancel`

Cancel a running pipeline. The run's summary gets status `cancelled`; its Ollama models are unloaded and its Docker containers killed, and its run workspace is removed unless the run saved a checkpoint (it can then be resumed). Returns HTTP `404` if the run is not running and HTTP `409` if it has already finished.

**Response:**
```json
{
  "status": "cancelled",
  "run_id": "a1b2c3d4e5f6"
}
```

#### `POST /api/runs/{run_id}/refine`

Start a new run that fixes the finished run's review issues: `POST /api/runs/{run_id}/retry` without `fresh_start` (the original request plus the issues of the run's latest review report; a plain rerun when there are none), without `strategy` in the response. It does not continue the old run; use `POST /api/runs/{run_id}/resume` for that.

**Response:**
```json
{
  "run_id": "f6e5d4c3b2a1",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/f6e5d4c3b2a1/events"
}
```

#### `GET /api/health`

Liveness check (used by the Docker health check).

**Response:**
```json
{
  "status": "ok",
  "version": "0.3.0"
}
```

#### `GET /api/health/full`

Full system health check including Ollama, Docker, disk space, and provider status.

**Response:**
```json
{
  "status": "ok",
  "version": "0.3.0",
  "server": { "uptime_seconds": null, "active_runs": 1, "auth_enabled": false },
  "ollama": { "running": true, "models": ["qwen3-coder:30b"], "url": "http://localhost:11434" },
  "docker": { "available": true, "version": "24.0.7" },
  "disk": { "free_gb": 45.2, "total_gb": 476.9, "used_percent": 90.5 },
  "providers": { "anthropic": true, "openai": true, "gemini": false }
}
```

#### `GET /api/system/stats`

Live system statistics: models loaded in Ollama (`/api/ps`), running Docker containers, and the server's active runs.

**Response:**
```json
{
  "gpu": {
    "models_loaded": 1,
    "models": [{ "name": "qwen3-coder:30b", "size_vram_gb": 17.3, "context_length": 32768 }],
    "total_vram_gb": 17.3
  },
  "docker": {
    "containers_running": 1,
    "containers": [
      { "id": "4b1e2c3d4f5a", "image": "python:3.12-slim", "status": "Up 2 minutes", "name": "...", "cpu": "25.00%", "mem": "512MiB / 15.5GiB" }
    ],
    "mode": "linux"
  },
  "pipeline": { "active_runs": 1, "run_ids": ["a1b2c3d4e5f6"], "gpu_available": false }
}
```

#### `POST /api/ollama/restart`

Unload every Ollama model, stop the Ollama server and start `ollama serve` again (on `localhost:11434`), for local models stuck in a bad state. Needs the `admin` role when authentication is on (HTTP `403` otherwise); HTTP `500` if the restart fails.

**Response:**
```json
{
  "status": "restarted",
  "models_available": 4,
  "message": "Ollama restarted successfully"
}
```

#### `GET /api/queue`

The runs the server is executing. The server runs one pipeline at a time; `gpu_available` is `false` while that slot is taken, and further runs wait for it.

**Response:**
```json
{
  "running": ["a1b2c3d4e5f6"],
  "running_count": 1,
  "gpu_available": false
}
```

#### `GET /api/stream-token`

Get a short-lived signed URL for a run's SSE stream or download, so a browser never puts the API key in a query string. Pass the target as `path` (it must start with `/api/runs/`, HTTP `400` otherwise): `GET /api/stream-token?path=/api/runs/a1b2c3d4e5f6/events`. The URL carries an HMAC-SHA256 signature (keyed by `DELEGATRON_STREAM_SECRET`) over the path, expiry and the caller's identity, and expires after 300 seconds.

**Response:**
```json
{
  "url": "/api/runs/a1b2c3d4e5f6/events?sig=...&expires=1776171600&uid=alice&tid=bc42c07d4dd4&role=admin",
  "expires_in_seconds": 300
}
```

#### `GET /api/analytics/leaderboard`

Preset performance across runs (of the caller's team when authentication is on), sorted by approval rate, then by average cost. `approval_rate` is the share of runs with `is_approved`, `error_rate` the share that ended `error`.

**Response:**
```json
[
  {
    "preset": "default",
    "runs": 15,
    "approval_rate": 0.87,
    "error_rate": 0.07,
    "avg_cost": 0.42,
    "total_cost": 6.3,
    "avg_iterations": 2.3
  }
]
```

### Settings

Settings changed here apply to the running server process only and are lost on restart. When authentication is on they need the `admin` role (HTTP `403` otherwise).

#### `PUT /api/settings/log-level`

Change the server log level at runtime: `DEBUG`, `INFO`, `WARNING` or `ERROR` (HTTP `422` otherwise).

**Request:**
```json
{
  "level": "DEBUG"
}
```

**Response:** `{"status": "ok", "level": "DEBUG"}`

#### `PUT /api/settings/log-timezone`

Switch log timestamps between `utc` and `local` (HTTP `422` otherwise).

**Request:**
```json
{
  "timezone": "local"
}
```

**Response:** `{"status": "ok", "timezone": "local"}`

#### `PUT /api/settings/parallel-tasks`

Enable or disable parallel tasks (`DELEGATRON_PARALLEL_TASKS`) for runs started afterwards: the first patches of a plan's independent tasks are generated concurrently, each in a git worktree of the run branch. A `parallel_tasks` set in a run's config file takes precedence.

**Request:**
```json
{
  "enabled": true
}
```

**Response:** `{"parallel_tasks": true}`

#### `PUT /api/settings/project-mode`

Enable or disable multi-milestone project mode (`DELEGATRON_PROJECT_MODE`), which `POST /api/projects` requires.

**Request:**
```json
{
  "enabled": true
}
```

**Response:** `{"project_mode": true}`

### Projects (Multi-Milestone)

#### `POST /api/projects`

Start a multi-milestone project. A meta-architect decomposes the request into milestones and each runs through the full pipeline, in one workspace that accumulates their code: a copy of the server's workspace, `<server workspace>/.delegatron-runs/<project_id>`. Project mode must be enabled (`PUT /api/settings/project-mode`), else HTTP `403`; HTTP `400` without `request`.

**Request:**
```json
{
  "request": "Build a full REST API with auth, database, and tests",
  "preset": "default",
  "max_milestones": 10,
  "max_iterations": 5,
  "max_cost_usd": 50.0,
  "max_wall_time_minutes": 120
}
```

The values shown are the defaults. `max_iterations` applies per milestone; `max_cost_usd` and `max_wall_time_minutes` to the whole project.

**Response:**
```json
{
  "project_id": "proj-0a1b2c3d4e5f",
  "status": "running",
  "message": "Project started (default). Track at /api/projects/proj-0a1b2c3d4e5f"
}
```

#### `GET /api/projects/{project_id}`

Get project status and milestone results. `status` is `running`, then `completed` (every milestone approved), `completed_partial` or `failed`. HTTP `404` if the project does not exist.

**Response:**
```json
{
  "project_id": "proj-0a1b2c3d4e5f",
  "status": "completed",
  "request": "Build a full REST API with auth, database, and tests",
  "preset": "default",
  "milestones_completed": 3,
  "milestones_total": 3,
  "plan": {
    "summary": "...",
    "milestones": [
      { "id": "m1", "title": "Database models", "description": "...", "acceptance_criteria": [...], "depends_on": [], "order": 1 }
    ],
    "total_estimated_iterations": 9
  },
  "results": [
    {
      "milestone_id": "m1",
      "title": "Database models",
      "status": "approved",
      "iterations": 2,
      "cost_usd": 0.31,
      "elapsed_seconds": 240.5,
      "emcs_score": 0.92,
      "files_created": ["models.py"],
      "files_deleted": [],
      "error": "",
      "task_outcomes": [...],
      "branch": "delegatron/run-..."
    }
  ],
  "total_cost_usd": 1.25,
  "total_time_seconds": 900.2,
  "completed_at": "2026-04-17T12:00:00+00:00"
}
```

#### `GET /api/projects`

List all projects (in reverse order of their ids, not by date): `project_id`, `status`, `request` (first 100 characters), `preset`, `milestones_completed`, `milestones_total` and `total_cost_usd` of each.

### SWE-bench Evaluation

#### `POST /api/swebench`

Start a SWE-bench evaluation run against real GitHub issues, as `delegatron swebench` does. The evaluation works in `<server workspace>/.delegatron-runs/<eval_id>`.

**Request:**
```json
{
  "preset": "local",
  "split": "lite",
  "instances": 10,
  "tier": "simple",
  "max_iterations": 5
}
```

All fields are optional: `preset` defaults to `"default"`, `split` to `"lite"` (`"lite"`, `"verified"` or `"full"`), `instances` to `10` and `max_iterations` (per instance) to `5`; `tier` (`simple`, `moderate`, `complex` or `feature`, by the size of the reference patch) limits the instances to one complexity tier.

**Response:**
```json
{
  "eval_id": "swe-0a1b2c3d4e5f",
  "status": "running",
  "message": "SWE-bench evaluation started (10 instances, local). Track at /api/swebench/swe-0a1b2c3d4e5f"
}
```

#### `GET /api/swebench/{eval_id}`

Get evaluation status and results: `eval_id`, `status` (`running`, `complete` or `error`), `preset`, `split`, `total_instances`, `tier`, `results` (per instance: `instance_id`, `tier`, `resolved`, `status`, `iterations`, `cost_usd`, `elapsed_seconds`, `emcs_score`, `error`), `resolve_rate`, `total_cost_usd`, `by_tier`, and when complete `total_time_seconds`, `avg_iterations` and `completed_at` (`error` when it failed). HTTP `404` if the evaluation does not exist.

#### `GET /api/swebench`

List all SWE-bench evaluation runs (in reverse order of their ids): `eval_id`, `status`, `preset`, `split`, `total_instances`, `completed`, `resolve_rate` and `total_cost_usd` of each.

### Templates

Templates allow you to save reusable pipeline configurations with variable placeholders. Variables in the `request_pattern` use `{variable_name}` (or `${variable_name}`) syntax and are substituted at run time.

#### `GET /api/templates`

List all templates, sorted by name: the 30 built-in ones (`templates/*.json`) and those saved with `POST /api/templates` (in `<DELEGATRON_DATA_DIR>/templates`; a saved template with a built-in's name replaces it). `is_builtin` tells them apart; `preset`, `overrides`, `constraints` and `variables` are left out when empty.

**Response:**
```json
[
  {
    "name": "add-endpoint",
    "description": "Add a REST endpoint to the project",
    "request_pattern": "Add a {method} {path} endpoint that {behavior}",
    "preset": "default",
    "overrides": {
      "max_iterations": 3
    },
    "constraints": {
      "required_patterns": ["def test_"]
    },
    "variables": {
      "method": "GET",
      "path": "/example",
      "behavior": "returns 200"
    },
    "is_builtin": false
  }
]
```

#### `POST /api/templates`

Save a new template.

**Request:**
```json
{
  "name": "add-endpoint",
  "description": "Add a REST endpoint to the project",
  "request_pattern": "Add a {method} {path} endpoint that {behavior}",
  "preset": "default",
  "overrides": {
    "max_iterations": 3,
    "architect": {
      "model": "claude-sonnet-5-5"
    }
  },
  "constraints": {
    "required_patterns": ["def test_"]
  },
  "variables": {
    "method": "GET",
    "path": "/example",
    "behavior": "returns 200"
  }
}
```

`name` is required. All other fields are optional. When a run starts from the template (`POST /api/templates/{name}/run`), `overrides` supplies `max_iterations`, `max_cost_usd`, `max_wall_time_minutes` and `interactive`, and `pipeline.docker_image` (the form the built-in templates use); other keys, such as per-role objects (`architect`, `implementer`, `reviewer`), are saved but not applied. The `variables` object provides default values for placeholders in `request_pattern`; their names must be valid identifiers.

**Response:**
```json
{
  "status": "saved",
  "name": "add-endpoint"
}
```

#### `DELETE /api/templates/{name}`

Delete a saved template by name. Built-in templates cannot be deleted.

**Response:**
```json
{
  "status": "deleted",
  "name": "add-endpoint"
}
```

Returns HTTP `404` if no saved template has that name (also for a built-in one).

#### `POST /api/templates/{name}/run`

Start a pipeline run from a saved template. Variables provided in the request body override the template's default variable values.

**Request:**
```json
{
  "variables": {
    "method": "POST",
    "path": "/users",
    "behavior": "creates a new user"
  },
  "preset": null,
  "workspace": "/path/to/repo",
  "sandbox": "docker"
}
```

All fields are optional. If `variables` is omitted, the template's default variables are used; a placeholder with no value is left in the request as `${name}`. The run uses the template's `preset`, else the body's `preset`, else `"local"`. The template's `constraints` are applied to the run (and kept for retry and resume).

**Response:**
```json
{
  "run_id": "a1b2c3d4e5f6",
  "status": "started",
  "message": "Pipeline started. Stream events at /api/runs/a1b2c3d4e5f6/events"
}
```

Returns HTTP `404` if the template does not exist. Returns HTTP `422` if the template's settings (for example its constraints) are invalid, and the errors of `POST /api/runs` otherwise.

### Benchmarks

Benchmark endpoints allow you to run the same task against multiple presets and compare results (cost, iterations, pass rate).

#### `POST /api/benchmark`

Start a benchmark comparison run. The task is executed against each preset sequentially, and results are stored for retrieval. Each preset's run has the id `<benchmark_id>-<preset>`, works in its own copy of the server's workspace (`<server workspace>/.delegatron-runs/<run id>`) and saves its artifacts like any run.

**Request:**
```json
{
  "request": "Add a /health endpoint returning {status: ok}",
  "presets": ["budget", "default", "claude-heavy"],
  "max_iterations": 5,
  "sandbox": "docker",
  "docker_image": null
}
```

`request` and `presets` (at least one) are required. `max_iterations`, `sandbox` (`docker` or `e2b`) and `docker_image` are optional overrides applied to all preset runs.

**Response:**
```json
{
  "benchmark_id": "bench-abc123def456",
  "status": "running",
  "message": "Benchmark started with 3 presets. Track at /api/benchmark/bench-abc123def456"
}
```

Returns HTTP `422` if any preset name is unknown.

#### `GET /api/benchmark/{benchmark_id}`

Get benchmark status and results. While the benchmark is running, partial results are returned as each preset completes.

**Response (running):**
```json
{
  "benchmark_id": "bench-abc123def456",
  "status": "running",
  "request": "Add a /health endpoint returning {status: ok}",
  "presets": ["budget", "default", "claude-heavy"],
  "results": [
    {
      "preset": "budget",
      "status": "approved",
      "iterations": 2,
      "cost_usd": 0.18,
      "elapsed_seconds": 45.3,
      "approved": true,
      "error": ""
    }
  ],
  "cheapest_passing": null
}
```

**Response (complete):**
```json
{
  "benchmark_id": "bench-abc123def456",
  "status": "complete",
  "request": "Add a /health endpoint returning {status: ok}",
  "presets": ["budget", "default", "claude-heavy"],
  "results": [
    {
      "preset": "budget",
      "status": "approved",
      "iterations": 2,
      "cost_usd": 0.18,
      "elapsed_seconds": 45.3,
      "approved": true,
      "error": ""
    },
    {
      "preset": "default",
      "status": "approved",
      "iterations": 3,
      "cost_usd": 0.42,
      "elapsed_seconds": 72.1,
      "approved": true,
      "error": ""
    },
    {
      "preset": "claude-heavy",
      "status": "approved",
      "iterations": 4,
      "cost_usd": 1.05,
      "elapsed_seconds": 120.8,
      "approved": true,
      "error": ""
    }
  ],
  "cheapest_passing": "budget"
}
```

Each result's `status` is the final status of that preset's run (`approved`, `partial`, `exhausted`, ...), or `error` with the `error` text when that run raised; the benchmark's own `status` is `running`, then `complete` (or `error`). The `cheapest_passing` field identifies the preset with the lowest cost among those whose runs were approved. It is `null` while the benchmark is still running or if no preset produced an approved result.

Returns HTTP `404` if the benchmark ID is not found.

### Teams and API Keys

The server has no REST endpoints for teams or API keys. Teams and keys are managed in Python, as shown under [Authentication](#authentication).

---

## Python SDK

You can also run the pipeline from Python. `delegatron.orchestrator.graph.Pipeline` is the same pipeline the CLI, the server and `RunQueue` run:

```python
import asyncio
from pathlib import Path
from typing import Any

from delegatron.config import load_config
from delegatron.orchestrator.graph import Pipeline


def on_step(step: str, data: dict[str, Any]) -> None:
    """Progress events: architect_complete, implementer_complete, tests_complete, ..."""
    if step != "token":  # "token" carries streamed model output (stream=True)
        print(step, data)


async def main() -> None:
    config = load_config(preset="default")  # or load_config(Path("delegatron.toml"))
    pipeline = Pipeline(
        config=config,
        workspace_root=Path("/path/to/repo"),
        run_id="my-run",
        step_callback=on_step,
        approval_callback=lambda plan: True,  # used when config.pipeline.interactive is on
    )
    state = await pipeline.execute("Add a health endpoint")
    print(state["status"], state["total_cost_usd"])


asyncio.run(main())
```

Artifacts go to `<workspace_root>/runs/<run_id>/` unless you pass `artifacts_dir`. In a git workspace the run commits to its own branch, `delegatron/run-<run_id>`, forked from `base_branch` (default: the checked-out branch); a workspace with uncommitted changes ends the run with status `error` ("Workspace setup failed") unless you pass `allow_dirty=True`, as the CLI's `--allow-dirty` does. To resume a run, pass its latest checkpoint: `pipeline.execute(saved["request"], initial_state=saved)` with `saved = ArtifactStore(base_dir, run_id).load_state()` (`delegatron.artifacts`).

`delegatron.orchestrator.async_runner.AsyncPipeline` is deprecated: it is an outdated copy of the pipeline loop, not a wrapper around `Pipeline`, and it emits a `DeprecationWarning`.

## Webhook Integration

### GitHub Webhook

`POST /api/webhooks/github` starts a run from a GitHub comment. There is no GitHub App: add a repository webhook that sends `Issue comments` events to this endpoint, with a secret equal to the server's `DELEGATRON_WEBHOOK_SECRET`.

1. Comment `/delegatron run <request>` on an issue or PR (the request may be up to 10,000 characters)
2. The server checks the `X-Hub-Signature-256` signature (HTTP `403` when it is wrong or `DELEGATRON_WEBHOOK_SECRET` is unset) and starts a run with that request on the server's default workspace and default configuration, as `POST /api/runs` would with no other fields
3. The run commits to its own branch; it opens a pull request only when the configuration enables `create_pr` (for example `DELEGATRON_CREATE_PR=true`) and the run ends `approved`

The response is `{"status": "started", "run_id": "...", "trigger": "issue_comment"}`, or `{"status": "ignored", "event": "<X-GitHub-Event>"}` for other events and comments. The run does not clone the repository the comment came from. Webhook runs are disabled (HTTP `403`) when authentication is on.

### Generic Webhooks

Send a POST to `/api/runs` (with your API key when authentication is on) for integration with any CI/CD system.

### Failure Alerts

When `DELEGATRON_ALERT_WEBHOOK_URL` is set, the server POSTs a JSON alert for every run that ends `error`, `exhausted`, `stalled`, `partial` or `cancelled`: `{"event": "run_failed", "run_id", "status", "request" (first 200 characters), "timestamp", "dashboard_url"}`. A failed alert is logged and never fails the run. The zero-cloud report of a run that ends `error`, `exhausted`, `stalled` or `partial` records the alert as egress.
