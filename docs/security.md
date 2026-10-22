# Delegatron Security Guide

Delegatron runs three patch checks on every implementer patch — **secrets scanning**, **dependency auditing** and **project constraints** — and produces a **zero-cloud report** for every run.

All patch checks judge only what the run introduces. Implementers return whole files in `files` or search/replace `edits` (folded into the file they change before the checks run); each file is compared with what it held before the run first changed it, so lines that were already there are never attributed to the patch, and a file returned unchanged is not a change at all. Each pass covers the files of the current patch and of the current task, so a finding that an earlier, already applied patch introduced is reported again until its line is removed. Every finding names the file and its line number after the patch is applied.

## Secrets Scanner

All generated code is scanned for leaked credentials before being committed.

### What It Detects

| Pattern | Severity | Example |
|---------|----------|---------|
| AWS access keys | Critical | `AKIA...` (20 uppercase chars) |
| GitHub tokens | Critical | `ghp_...`, `gho_...`, `ghu_...`, `ghs_...`, `ghr_...` |
| GitHub PATs | Critical | `github_pat_...` |
| Stripe keys | Critical | `sk_live_...`, `sk_test_...`, `rk_live_...` |
| Anthropic API keys | Critical | `sk-ant-...` |
| OpenAI project keys | Critical | `sk-proj-...` |
| Private keys (PEM) | Critical | `-----BEGIN RSA PRIVATE KEY-----` |
| Connection strings | High | `postgres://user:pass@db.example.com/db` — only with a password and a non-loopback host |
| Generic API keys | High | `api_key = "<20+ char quoted literal>"` |
| Generic tokens / passwords | High | `token = "eyJhbGci..."`, `password = "..."` |
| High-entropy strings | Medium | Entropy >= 4.5 (base64) or >= 3.5 (hex) in assignment context |

Generic keys, tokens and passwords must be quoted literals: `token = create_token(user)` or `api_key = settings.api_key` is code, not a secret. In env, ini, cfg, conf, properties and YAML files an unquoted value that fills the rest of the line also counts. Values containing `{`, `}`, `$`, `<` or `>` are treated as template placeholders, and connection strings to `localhost`, `127.0.0.0/8` or `::1` are ignored.

### How It Works

1. The scanner runs after the implementer produces a `PatchSet`, before the patch is applied
2. Only lines the run **adds or changes** in the current patch's and task's files are scanned (see above)
3. Each line is checked against the patterns above and an entropy detector; at most one finding per line
4. Findings include the file, line number, pattern name, severity, and a **redacted** line (the text before the secret plus its first 4 characters, then `****REDACTED****`)
5. What happens next depends on the [security mode](#security-modes)

### Allowlisting False Positives

Suppress known false positives (test fixtures, example values) with regex patterns:

```toml
# delegatron.toml
[security]
secrets_allowlist = [
    "EXAMPLE_KEY_DO_NOT_USE",
    "test_api_key_\\d+",
    "sk_test_fixture.*"
]
```

A line that matches any allowlist regex is skipped; an invalid regex is ignored with a warning. The orchestrator passes the list to `run_security_checks(..., secrets_allowlist=...)`.

### CLI Usage

Secrets scanning runs automatically during `delegatron run`. Findings appear in the event log (`secrets_detected`, with the affected files) and are fed back to the implementer and reviewer. Use `delegatron review --focus security` for a security-focused read-only audit.

## Dependency Auditor

New third-party imports introduced by generated code are flagged for review.

### How It Works

1. The auditor extracts `import` and `from ... import` statements from the lines the patch adds to `.py` files (other languages are covered by the import validator)
2. Each import is classified as:
   - **Stdlib** — Python standard library, or `pytest` (always installed in the sandbox) (ignored)
   - **Declared** — Declared in `pyproject.toml` (PEP 621 dependencies and optional dependencies, PEP 735 `[dependency-groups]`, Poetry's main, dev and `group.*` tables, `[tool.pdm.dev-dependencies]`, `[tool.uv] dev-dependencies`), `setup.cfg` (`install_requires`, `extras_require`), or any root `requirements*.txt` or `requirements/*.txt`, following `-r` / `--requirement` includes that stay inside the workspace (ignored). Manifests the same patch adds or changes count; UTF-16 manifests are read correctly
   - **Local** — A module or package in the workspace or in the patch itself (ignored)
   - **New** — None of the above (flagged with severity `warning`)
3. Package-name-to-import-name mappings are handled (e.g., `pillow` -> `PIL`, `scikit-learn` -> `sklearn`, `pyyaml` -> `yaml`)
4. Relative imports are skipped (project-internal)

### What Gets Flagged

```
New dependency detected: requests (from: import requests)
New dependency detected: boto3 (from: import boto3)
```

Each new top-level module is reported once, at the first added line that imports it, as the finding `New dependency: <name>`. New dependencies are logged as `new_dependencies` events. They are `minor` findings: advisory in every security mode, never blocking.

## Zero-Cloud Verification

Prove that no code or data left your local network during a pipeline run. This is Delegatron's key differentiator for privacy-sensitive environments.

### How It Works

The orchestrator records network interactions in a `NetworkGuard` as it makes them:

1. **LLM calls** — provider, model, and endpoint URL of each agent invocation
2. **Sandbox calls** — the sandbox type; `docker`, `local` and `none` run on this machine, `e2b` and any other type are cloud
3. **Artifact storage** — where artifacts are persisted
4. **Other egress** — `record_external_call(kind, host)`: a `git push` for a pull request (`git_push`, classified by the remote's host), the `gh pr create` API call (`vcs_api`, the `GH_HOST` host, else `github.com`) and the server's failure alert to `DELEGATRON_ALERT_WEBHOOK_URL` for a run that ends `error`, `exhausted`, `stalled` or `partial` (`notification`). An `e2b` call is always cloud, whatever host it names

Each interaction is classified as **local** or **cloud** without DNS lookups (the same rule, minus the configured extra names, decides whether a model endpoint is local for cost estimates, prompt versions and timeouts):

- **Local IP literals**: loopback (`127.0.0.0/8`, `::1`), private (`10/8`, `172.16/12`, `192.168/16`, IPv6 `fc00::/7`), link-local, carrier-grade NAT (`100.64.0.0/10`, e.g. Tailscale), and `0.0.0.0`
- **Local names**: `localhost`, `host.docker.internal`, names ending in `.local`, `.localhost`, `.lan`, `.internal` or `.home.arpa` (including `*.svc.cluster.local`), single-label names such as a compose service `ollama`, and names listed in `DELEGATRON_LOCAL_HOSTS` or `[pipeline] local_hosts` (comma-separated / a list)
- **Cloud**: any other host. LLM calls through the `anthropic`, `openai`, `google` and `litellm` providers are always flagged, whatever their `base_url`; the `mock` provider is always local. A name such as `10.evil.example.com` is a DNS name, not a private address

Using the E2B sandbox breaks zero-cloud operation, and so does `create_pr = true` (which pushes the run branch and calls the GitHub API) unless both the git remote's host and `GH_HOST` are on your network; the report says so.

### Zero-Cloud Report

After a run completes, `NetworkGuard.finalize()` produces a `ZeroCloudReport`, saved as `zero_cloud_report.json` in the run's artifact directory; `summary.json` gets a short `zero_cloud` entry (`verified`, `violations`, `fingerprint`):

```json
{
  "run_id": "run-abc123",
  "cloud_free": true,
  "timestamp": "2026-04-09T12:00:00+00:00",
  "total_events": 7,
  "events": [
    {
      "timestamp": "2026-04-09T12:00:01+00:00",
      "category": "llm_call",
      "role": "architect",
      "provider": "openai-compatible",
      "host": "localhost",
      "is_local": true,
      "details": {"model": "phi4:14b", "base_url": "http://localhost:11434/v1"}
    }
  ],
  "violations": [],
  "summary": {
    "total_events": 7,
    "local_events": 7,
    "cloud_events": 0,
    "by_category": {"llm_call": 5, "sandbox": 1, "artifact_store": 1},
    "by_provider": {"openai-compatible": 5, "docker": 1, "local_fs": 1},
    "enforcement_mode": "audit"
  },
  "fingerprint": "3f9a..."
}
```

The report is an audit: nothing is blocked, and `enforcement_mode` is always `audit`.

The **fingerprint** is the SHA-256 of the whole report (canonical JSON with sorted keys, minus the `fingerprint` field), so changing any field of any event changes it. Recompute it with `delegatron.security.zero_cloud.report_fingerprint(report)`. It is an integrity checksum, not a signature: anyone who can edit the file can also recompute it.

### Accessing the Report

```bash
# CLI: use Ollama presets for zero-cloud operation
delegatron run --preset local "your task"

# API: get compliance report for a completed run
curl http://localhost:8000/api/runs/{run_id}/compliance

# Dashboard: visit /compliance page for visual verification
```

### Dashboard Compliance Page

The `/compliance` page provides:

- **Green/red verdict** — whether the run was fully local
- **Event log** — every network interaction with timestamps
- **Provider breakdown** — which providers were used and whether they're local
- **Fingerprint display** — SHA-256 hash for audit records
- **JSON export** — download the full report

### Achieving Zero-Cloud Operation

To guarantee no data leaves your network:

1. Use a local preset (`local`, `local-phi4`, `local-heavy`, `local-oss`, `local-oss-review`)
2. Use the Docker sandbox (default) — runs on your machine. Not E2B
3. Leave `create_pr` off, or use a GitHub Enterprise server on your network: both the `origin` remote's host and `GH_HOST` must be local
4. Artifacts are stored locally in a `runs/` directory

```bash
# Fully local — zero cloud contact
delegatron run --preset local "Add input validation"

# Verify
delegatron show <run-id>  # lists the run's artifacts, including zero_cloud_report.json
cat runs/<run-id>/zero_cloud_report.json  # "cloud_free": true
```

### Security Modes

All three patch checks (constraints, secrets, dependencies) run in sequence via `run_security_checks()`, and results are aggregated into a `SecurityResult`. `SecurityResult.findings` lists every finding as a review issue — `{severity, title, details, file, line, source}` — with this severity:

| Finding | Severity |
|---------|----------|
| Critical secret (private key, AWS / GitHub / Stripe / Anthropic / OpenAI key) | `blocker` |
| Any other secret | `major` |
| Constraint violation (`no_modify`, `forbidden_patterns`, `required_patterns`) | `major` |
| New dependency | `minor` |

`source` is `secrets`, `constraints` or `dependencies`.

`security_mode` (`[pipeline]`, or a run template's `pipeline` overrides; `allow`, `warn` or `block` in any case, anything else is a configuration error) decides what the orchestrator does with them:

| Mode | Behavior |
|------|----------|
| `block` | A patch with any blocker or major finding is rejected. It is not written to the workspace or committed (`patch_applied` is false), a `security_blocked` event lists each finding's title, severity and file, and the implementer gets the findings with its next attempt. While a blocking finding is open the task cannot be approved or advance, and no pull request is opened. A finding never ends the run by itself: a task that keeps producing blocked patches ends through the usual iteration, stall and budget limits |
| `warn` (default) | The patch is applied. Findings are shown to the implementer and reviewer as advisories and logged as events; the orchestrator does not block on them |
| `allow` | Checks run and their events are logged; findings are not passed to the agents |

`blocking_findings(result, mode)` returns the findings that block in the given mode: the blocker and major ones in `block` mode, none otherwise. Minor findings (new dependencies) never block.

### Security in `summary.json`

Every run's `summary.json` has a `security` section with the mode and the findings still open when the run ended:

```json
"security": {
  "mode": "block",
  "unresolved_findings": [
    {
      "severity": "blocker",
      "title": "Possible secret: aws_access_key",
      "details": "Line matches the aws_access_key pattern: AWS_KEY = \"AKIA****REDACTED****",
      "file": "app/config.py",
      "line": 3,
      "source": "secrets"
    }
  ],
  "unresolved": {"by_kind": {"secrets": 1}, "by_severity": {"blocker": 1}},
  "unresolved_blocking": 1,
  "detected": {"by_kind": {"secrets": 1, "constraints": 1}, "by_severity": {"blocker": 1, "major": 1}},
  "blocked_patches": 2
}
```

`unresolved_findings` uses the finding shape above and is empty in `allow` mode. It comes from a final scan of everything the run changed, judged against what each file held before the run first touched it (a per-run baseline, rebuilt from the run branch's fork point on resume), so a finding stays reported until its line is actually gone, whether or not later patches touch the file again. `unresolved` counts those findings by kind and severity and `unresolved_blocking` the ones that block in `block` mode; `detected` counts every distinct finding the run met on the way, and `blocked_patches` the patches `block` mode kept out of the workspace. A pull request is not opened while blocking findings remain.

## Sandbox Isolation

With the Docker (default) or E2B sandbox, generated code never runs on the host. The `local` sandbox (`none` is an alias) runs it directly on the host with no filesystem or network isolation; use it only on machines you consider disposable (test VMs, CI runners).

| Backend | Isolation | Network | Resource Limits |
|---------|-----------|---------|----------------|
| **Docker** | Container | `--network none` during tests | 512MB RAM, 1 CPU, 600s timeout per container |
| **E2B** | Firecracker microVM | Hardware-level | Configurable per E2B plan; 300s sandbox and command timeout |
| **Local** (`local` / `none`) | None — runs on the host | Host network | 300s per command |

Entrypoint smoke runs (`run_entrypoints`) are on by default only in the Docker and E2B sandboxes; in the local sandbox the scripts would run on the host.

### Docker Security Hardening

- **Network isolation**: Install phase has network; test phase uses `--network none`
- **Path containment**: `_validate_sandbox_path()` prevents writes outside sandbox directory
- **Command sanitization** (every sandbox): `sanitize_shell_command()` rejects an agent-supplied test command containing `;`, backticks, `$`, `|`, `>`, `<`, `\`, `&`, `../` or a control character such as a newline (`UnsafeCommandError`), so a blocked command never runs in any form. Commands Delegatron builds itself (project detection, toolchain rules) are trusted and not filtered
- **No fallback**: If Docker is unavailable, the sandbox refuses to run (never falls back to host execution)

### Path Traversal Protection

Multiple layers prevent path traversal attacks:

- `utils.paths.normalize_rel_path()` — every model-supplied path (patch `files`, `edits`, `delete_files`) is normalised before anything touches the disk; absolute, drive-letter and UNC paths, `..` segments, `.git` in any spelling (including `git~1`), `:` (NTFS streams), NUL and reserved Windows device names (`CON`, `nul.txt`) are rejected
- `Workspace.apply_files()` — skips a rejected path, or one a symlink leads out of the workspace, without aborting the rest of the patch; the implementer gets a `Rejected file path` blocker
- `Workspace.read_file()` — validates paths stay within repo boundary
- `DockerSandbox._validate_sandbox_path()` — file writes stay within sandbox
- `E2BSandbox._validate_e2b_path()` — rejects absolute paths and `..` components
- `ArtifactStore.__init__()` — validates run_id format and path containment
- `RunRequest.validate_config_path()` — blocks `..` and absolute paths
- `context.gather_context()` — prevents traversal when reading files for implementer

## Git URL Workspaces

`--workspace` (CLI) and `workspace` (API) accept a local directory or a git URL; the clone is cached under `~/.delegatron/repos/` and reused.

- **An existing path is always local**, even if it contains `github.com/` (a GOPATH checkout, for example).
- **Accepted URL forms**: `https://host/path`, `ssh://[user@]host[:port]/path`, `user@host:path`, and a bare `github.com/…`, `gitlab.com/…` or `bitbucket.org/…` (cloned over https). Anything else that names a remote is rejected before git runs: values starting with `-` (git options such as `--upload-pack=…`), whitespace or control characters, `ext::` and other transport helpers, `file:` URLs, and plain `http://`.
- **Cloning** runs `git clone [--branch B] -- <url> <dir>` with `GIT_ALLOW_PROTOCOL=https:ssh` and a timeout; a failed clone removes the directory it created. Without `--branch` the remote's default branch is cloned. Branch names must match `^[A-Za-z0-9._/][A-Za-z0-9._/-]*$`.
- **Reused clones** check out the requested branch (or the remote default, `origin/HEAD`) and fast-forward. A failed checkout is an error — the run never proceeds on the wrong branch; a failed pull (offline, auth) logs a warning with git's message and the commit in use, and the cached clone is used.
- **Server**: git hosts must be listed in `DELEGATRON_GIT_ALLOWED_HOSTS` (default `github.com,gitlab.com,bitbucket.org`), and local paths must lie under `DELEGATRON_WORKSPACE_ROOT` when it is set. Role overrides in API requests follow the rules in [Server role overrides](#server-role-overrides).

## Provider API Keys

A role's API key is, in order: its `api_key`; the environment variable its `api_key_env` names; the provider's default variable.

| Provider | Default key variable | Sent only to |
|----------|----------------------|--------------|
| `anthropic` | `ANTHROPIC_API_KEY` | `api.anthropic.com` |
| `openai` | `OPENAI_API_KEY` | `api.openai.com` |
| `litellm` | `OPENAI_API_KEY` | `api.openai.com` |
| `google` | `GEMINI_API_KEY` | `generativelanguage.googleapis.com` |
| `openai-compatible` | none | — |

- **A default key goes only to its provider's own API host.** A role whose `base_url` names another host does not get the default key; for a non-local host a warning, logged once per host and model, names the variable and the host.
- **`openai-compatible` has no default key.** Ollama and other local servers need none. A hosted OpenAI-compatible API (Groq, OpenRouter, xAI) needs `api_key_env` naming its key (a remote endpoint without one logs a warning), and so does an `openai` or `litellm` role pointed at another host (Azure OpenAI, a LiteLLM proxy).
- **Explicit settings are honored for any host.** An `api_key` or `api_key_env` from a preset, `delegatron.toml` or the CLI is the operator's choice and is sent to the role's `base_url`, wherever it points.

### Server role overrides

API requests can override a role's settings, including its `provider`, `base_url` and `api_key_env`. This applies to `POST /api/runs`, `POST /api/runs/plan`, `POST /api/review`, `POST /api/estimate` and `POST /api/runs/{id}/retry`; `/rerun`, `/resume` and `/refine` replay the overrides saved with the run and are checked the same way. The server never hands its credentials to an endpoint the caller chose:

- **`base_url`** must be an `http://` or `https://` URL with a host; anything else is rejected with HTTP 422. When an override sends a role to a `base_url` that is not trusted, the server gives that role no key at all: no configured `api_key`, no environment key (nor an `ANTHROPIC_AUTH_TOKEN` from its environment). Trusted endpoints are the `base_url`s of the roles in the operator's preset or TOML config, plus the URLs listed in `DELEGATRON_ALLOWED_BASE_URLS` (comma-separated), compared by scheme, host and port.
- **`api_key_env`** may name only the provider's default variable, the variable the operator configured for that role, or a variable listed in `DELEGATRON_ALLOWED_KEY_ENVS` (comma-separated). Any other name, for example `DELEGATRON_STREAM_SECRET`, is rejected with HTTP 422 before a run is created.
- **An override that changes a role's `provider` or `base_url`** starts from a clean endpoint: the operator's `base_url`, `api_key`, `api_key_env` and prompt version for that role are dropped unless the override sets them.
- The 422 detail names the role and the field (for example `architect.api_key_env`). It never contains a key value or the allow-lists.
- **`config_path`** (`POST /api/runs`) loads a TOML file as operator config, so its roles keep their keys. It must be a relative path inside the server's workspace root, and it may not lie in a Delegatron directory (`.delegatron-runs/` and the other internal directories, or the run artifacts): a file an earlier run wrote there is never trusted as operator config (HTTP 422).

## Project-Level Constraints

Define rules that generated code must follow:

```toml
# delegatron.toml
[constraints]
# Paths that must never be modified or deleted (gitignore-style)
no_modify = ["alembic/versions/", ".env", "package-lock.json"]

# Text that at least one changed source file must contain
required_patterns = ["def test_"]

# Text that must NOT appear in lines the patch adds
forbidden_patterns = ["eval(", "exec(", 're:os\.system\(', "# TODO"]

# Prose rules for the agents; never checked mechanically
guidelines = ["Use type hints on public functions"]
```

- **Patterns are literal text** by default: `eval(` matches the characters `eval(`, and `import *` does not match `import os`. Prefix an entry with `re:` for a regular expression (`re:\beval\(`). An invalid `re:` entry is a configuration error, reported when the constraints are loaded — it is never silently skipped.
- **`forbidden_patterns`** are matched line by line against the lines the patch adds; existing code is not reported.
- **`required_patterns`** are satisfied when any changed, non-empty source file contains the pattern; a patch that changes no source files is not checked. Each unmet pattern is one violation per patch. A required `re:` pattern is searched in the whole file, so `^` and `$` match only at the start and end of the file; write `re:(?m)^import os` to match at the start of any line. (Forbidden patterns are matched line by line, so `^` there means the start of a line.)
- **`no_modify`** matching is case-sensitive and identical on every platform; `\` and a leading `./` are normalised. A trailing `/` protects a directory (`alembic/versions/` protects `alembic/versions/sub/x.py`), a pattern without `/` matches any path component (`.env` protects `config/.env` but not `.envrc`), a leading `/` anchors to the workspace root, and `**/` spans zero or more directories (`src/**/*.py` matches `src/a.py`). In a pattern that contains a `/`, `*` and `?` also match `/`, so `docs/*.md` protects `docs/a/b.md` too. This is stricter than gitignore, never looser.
- **`guidelines`** are shown to the agents and never produce violations.

Constraints from `delegatron.toml`, from a run template and from an API request are merged with `delegatron.constraints.merge_constraints`. Violations are checked on each implementer patch before it is applied, logged as `constraint_violation` events, and handled according to the [security mode](#security-modes).
