# Delegatron Deployment Guide

## Prerequisites

| Component | Version | Required? |
|---|---|---|
| Python | 3.11+ | Yes |
| Docker | 20.10+ | Yes (for sandbox execution) |
| Node.js | 18+ | Only for dashboard |
| npm | 9+ | Only for dashboard |

You need at least one LLM provider API key, or a local Ollama install for the `local*` presets (which need no key). The `budget` preset uses only Google (Gemini), so a single `GEMINI_API_KEY` is enough to get started. The `default` preset uses all three providers.

## Windows — Clean Install

For a fresh Windows machine with nothing installed, use the bootstrap script (PowerShell as Administrator):

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
iwr -useb https://raw.githubusercontent.com/borkit/delegatron/main/bootstrap-windows.ps1 | iex
```

This installs the prerequisites via winget (Python and Git; Docker, Ollama and Node.js when you accept their prompts), clones the repo, and runs the full setup. See [bootstrap-windows.ps1](../bootstrap-windows.ps1).

## Local Development Setup

```bash
git clone https://github.com/borkit/delegatron.git
cd delegatron
pip install -e ".[dev,cloud-providers]"   # cloud-providers: the Anthropic and Google SDKs
```

The `cloud-providers` extra is needed for any role on the `anthropic` or
`google` provider, including the `default` and `budget` presets.

Create a `.env` file or export environment variables (a `.env` in the
checkout root is loaded at startup and overrides variables already set):

```bash
# At minimum, one provider key:
export ANTHROPIC_API_KEY=sk-ant-...
# Or for budget mode (Gemini-only):
export GEMINI_API_KEY=...
```

Verify installation:

```bash
delegatron --version
delegatron list-presets
```

## Production Deployment (API Server)

### Running with uvicorn

The API server is a FastAPI app served by uvicorn:

```bash
# Direct
delegatron serve --host 0.0.0.0 --port 8000 --workspace /path/to/repos

# Or via uvicorn directly (for process managers like systemd/supervisord)
uvicorn delegatron.server.app:app --host 0.0.0.0 --port 8000
```

When serving with uvicorn directly, set the workspace (and optionally the
artifacts directory) in the environment; `delegatron serve` takes the
workspace from `--workspace` (default: the current directory) and also reads
`DELEGATRON_ARTIFACTS_DIR`:

```bash
export DELEGATRON_WORKSPACE=/path/to/repos       # runs work in <workspace>/.delegatron-runs/
export DELEGATRON_ARTIFACTS_DIR=/var/lib/delegatron/runs   # default: <install dir>/runs
uvicorn delegatron.server.app:app --host 0.0.0.0 --port 8000
```

Without `DELEGATRON_WORKSPACE`, plain uvicorn uses the current directory as the
workspace and `./runs` for artifacts (`DELEGATRON_ARTIFACTS_DIR` is then
ignored). A `--workspace` or `DELEGATRON_WORKSPACE` that is the Delegatron
checkout itself is replaced by `~/delegatron-workspace`.

The server needs `git` on `PATH`: every run works in its own git workspace,
and git-URL workspaces are cloned.

Run a single server process (uvicorn's default of one worker): active runs,
SSE streams and pending plan approvals are held in memory. The server executes
one run at a time; further runs are queued until the current one finishes.

### Data directory

The server stores API keys and team data in a data directory:

```bash
export DELEGATRON_DATA_DIR=/var/lib/delegatron/data  # default: ./data
```

Contents:
- `delegatron.db` -- SQLite database: hashed API keys, team membership
- `templates/`, `projects/`, `benchmarks/`, `swebench/` -- user templates and job records

### Health check

```bash
curl http://localhost:8000/api/health
```

Returns `{"status": "ok", "version": "..."}`. Use this for load balancer health
probes. `GET /api/health/full` also reports Ollama, Docker, disk and active runs.

### Running behind a reverse proxy

In production, put the API server behind nginx, Caddy, or a cloud load balancer with TLS termination. The server binds to `0.0.0.0:8000` by default.

## Docker Compose

### Quick start

```bash
git clone https://github.com/borkit/delegatron.git
cd delegatron
cp .env.example .env  # Edit with your API keys
docker compose up -d
```

Create the bind-mounted directories first: the container runs as uid 1000,
and directories Docker creates for missing mounts are owned by root (or add
`user: "${UID}:${GID}"` to the service).

```bash
mkdir -p workspace runs data
```

### Services

The `docker-compose.yml` starts one service, **delegatron-api**: the API
server on port 8000. Runs execute inside it.

### Image

- The image includes `git` (run workspaces and git-URL workspaces need it).
- Python extras are chosen at build time with `DELEGATRON_EXTRAS` (default
  `cloud-providers`, the Anthropic and Google SDKs the default preset needs),
  e.g. `DELEGATRON_EXTRAS=cloud-providers,e2b docker compose build`.
- `DELEGATRON_WORKSPACE`, `DELEGATRON_ARTIFACTS_DIR` and `DELEGATRON_DATA_DIR`
  are preset to the mounted directories below.

### Volumes

| Host | Container | Contents |
|---|---|---|
| `./workspace` | `/app/workspace` | The workspace runs start from; each run works in `.delegatron-runs/<run_id>` |
| `./runs` | `/app/runs` | Run artifacts (plans, patches, reports, summaries) |
| `./data` | `/app/data` | SQLite database (API keys, teams) and templates |

### Sandbox

The image has no Docker CLI, so the default `docker` sandbox cannot start
containers as shipped. Choose one:

- **E2B**: set `DELEGATRON_SANDBOX=e2b` and `E2B_API_KEY`, and build with
  `DELEGATRON_EXTRAS=cloud-providers,e2b`.
- **Host Docker**: install the Docker CLI in a derived image, mount
  `/var/run/docker.sock`, and mount the workspace at the *same* path inside the
  container as on the host. The host daemon resolves the sandbox's bind mounts
  on the host, so the paths must match.

### Health check

```bash
curl http://localhost:8000/api/health
```

## Windows Containers

Delegatron can use Windows Docker containers for building and testing
Windows-specific code (.NET Windows Services, WPF apps, PowerShell modules).

### Requirements

- **Windows 11 Pro/Enterprise** (Home edition does not support Windows containers)
- **Docker Desktop 4.x+** with WSL 2 engine enabled
- **Windows Containers feature** enabled in Windows

### Setup

1. Enable the Windows Containers feature (admin PowerShell, requires reboot):
   ```powershell
   Enable-WindowsOptionalFeature -Online -FeatureName Containers -All
   ```

2. Restart your machine.

3. After reboot, Docker Desktop can run Windows containers. Use the
   `DELEGATRON_DOCKER_IMAGE` environment variable or template overrides
   to specify a Windows image:
   ```
   DELEGATRON_DOCKER_IMAGE=mcr.microsoft.com/dotnet/sdk:9.0-nanoserver-ltsc2022
   ```

### Recommended Windows images

| Image | Use case |
|---|---|
| `mcr.microsoft.com/dotnet/sdk:9.0-nanoserver-ltsc2022` | .NET / C# projects |
| `mcr.microsoft.com/windows/servercore:ltsc2022` | General Windows apps |
| `mcr.microsoft.com/powershell:lts-nanoserver-ltsc2022` | PowerShell modules |

### Switching Docker daemon mode (PowerShell)

If you are NOT using WSL 2 (legacy Hyper-V mode), you can switch between
Linux and Windows containers via PowerShell:

```powershell
# Switch to Windows containers
& "C:\Program Files\Docker\Docker\DockerCli.exe" -SwitchDaemon

# Check current mode
docker version --format '{{.Server.Os}}'
# Returns "windows" or "linux"
```

> **Note:** Switching modes restarts the Docker daemon and kills all running
> containers. With WSL 2 engine enabled, this command is not available —
> Docker runs Linux containers only unless the Windows Containers feature
> is separately enabled.

### Linux fallback

For C# / .NET projects that don't use Windows-specific APIs, the Linux
.NET SDK image works fine and is faster to pull:
```
DELEGATRON_DOCKER_IMAGE=mcr.microsoft.com/dotnet/sdk:9.0
```

This supports building, running, and testing .NET code without needing
Windows containers enabled.

## Kubernetes (Helm)

### Prerequisites

- Kubernetes 1.24+
- Helm 3.x
- Container registry access

### Install

No image is published: build one from the repository's `Dockerfile` and push
it to your registry.

```bash
docker build -t registry.example.com/delegatron:0.3.0 .
docker push registry.example.com/delegatron:0.3.0

# Install chart (the chart creates the <release>-secrets Secret from these values)
helm install delegatron ./helm \
  --set image.repository=registry.example.com/delegatron \
  --set image.tag=0.3.0 \
  --set secrets.anthropicApiKey=sk-ant-... \
  --set secrets.openaiApiKey=sk-... \
  --set secrets.geminiApiKey=...
```

### Configuration (values.yaml)

```yaml
replicaCount: 1

image:
  repository: delegatron
  tag: latest
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 8000

resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    cpu: "1"
    memory: 1Gi

config:            # rendered into the <release>-config ConfigMap
  maxIterations: "5"       # DELEGATRON_MAX_ITERATIONS
  maxCostUsd: "5.00"       # DELEGATRON_MAX_COST_USD
  maxWallTime: "60"        # DELEGATRON_MAX_WALL_TIME
  sandbox: docker          # DELEGATRON_SANDBOX
  logLevel: INFO           # DELEGATRON_LOG_LEVEL

secrets:           # rendered into the <release>-secrets Secret
  anthropicApiKey: ""
  openaiApiKey: ""
  geminiApiKey: ""
```

The chart sets only these variables. It has no ingress, no persistent volume
(run workspaces, artifacts and the SQLite database live in the pod and are lost
when it is replaced) and no Docker socket, so the default `docker` sandbox
cannot start containers in the pod. Variables such as `E2B_API_KEY`,
`DELEGATRON_AUTH_ENABLED` or `DELEGATRON_STREAM_SECRET` need an edit to
`helm/templates/deployment.yaml`.

### Scaling

Keep `replicaCount: 1`. The server holds active runs, SSE streams and pending
plan approvals in memory and executes one run at a time, so replicas do not
share work; the chart has no separate worker deployment.

## Dashboard Deployment

The dashboard is a Next.js 16 app (React 19, Tailwind CSS 4).

### Quick Start (Windows)

Use the one-click launcher:

```
start-dashboard.bat
```

This starts both the API server and dashboard, opens the browser, and provides clean shutdown.

### Build

```bash
cd dashboard
npm install
npm run build
```

`npm run build` writes a `.next/` build that `next start` serves;
`delegatron serve` starts it on the API port + 1 (e.g. 8001) when
`dashboard/.next` exists. The API server accepts browser requests only from
the origins in `DELEGATRON_CORS_ORIGINS` (default `http://localhost:3000`,
`:8001`, `:8002` and `:8081`); add the dashboard's origin when it is served
from anywhere else.

### Cloudflare Pages

> **Note (2026-10):** the dashboard is no longer a static export
> (`dashboard/next.config.js` has no `output: "export"`), so `npm run build`
> produces no `dashboard/out` and the Pages deploy below does not work as
> shipped. Host the dashboard with `next start` on a Node.js host instead; the
> Cloudflare Access steps apply to whichever hostname serves it.

The project includes a `wrangler.toml` pre-configured for Cloudflare Pages:

```bash
npx wrangler pages deploy dashboard/out --project-name=delegatron
```

Or connect your GitHub repo in the Cloudflare Pages dashboard:
- **Build command:** `cd dashboard && npm run build`
- **Output directory:** `dashboard/out`

Set the API URL in Cloudflare Pages environment variables:

```
NEXT_PUBLIC_API_URL=https://your-api-server.example.com
```

### Securing the dashboard with Cloudflare Access

The dashboard has no built-in password protection. API endpoints are
secured by Bearer tokens when `DELEGATRON_AUTH_ENABLED=true`, but the dashboard HTML/JS itself is
publicly accessible unless you add a gate. **Cloudflare Access** (free for up
to 50 users) adds authentication in front of the dashboard without any code
changes.

**Step 1 — Enable Zero Trust**

1. Log in to the [Cloudflare dashboard](https://dash.cloudflare.com)
2. Go to **Zero Trust** in the left sidebar (or visit `https://one.dash.cloudflare.com`)
3. If prompted, create a team name (e.g. `your-org`) — this sets your auth
   domain to `your-org.cloudflareaccess.com`

**Step 2 — Add the dashboard as an Access application**

1. In Zero Trust, go to **Access → Applications → Add an application**
2. Choose **Self-hosted**
3. Configure:
   - **Application name:** `Delegatron Dashboard`
   - **Session duration:** 24 hours (or your preference)
   - **Application domain:** your Pages domain (e.g. `delegatron-dashboard.pages.dev`)
   - **Path:** leave blank to protect the entire site

**Step 3 — Create an access policy**

1. **Policy name:** `Allowed users`
2. **Action:** Allow
3. **Include rule:** choose one of:
   - **Emails** — list each tester's email address
   - **Email domain** — allow everyone at your org domain (e.g. `@yourcompany.com`)
   - **GitHub organization** — allow members of a specific GitHub org
4. Save the policy, then save the application

**Step 4 — Verify**

Visit your dashboard URL. You should see a Cloudflare Access login page.
After authenticating (via one-time PIN email or your configured IdP), you'll
be redirected to the dashboard. Unauthenticated visitors are blocked.

**Optional — Protect the API too**

If your API server is also behind Cloudflare (e.g. via Cloudflare Tunnel), you
can add a second Access application for the API domain. Use a **Service Auth**
policy with service tokens for programmatic access, and email-based policies
for browser access.

> **Note:** Cloudflare Access free tier supports up to 50 users. For private
> deployment with a handful of trusted users, this is the simplest zero-code auth solution.

### Other hosting

The dashboard can also be deployed to any Node.js-capable host (Vercel, Railway, etc.). Set `NEXT_PUBLIC_API_URL` at build time to point to your API server, and add the dashboard's origin to the API server's `DELEGATRON_CORS_ORIGINS`.

## API Server Hosting with Cloudflare Tunnel

The Delegatron API server is a Python FastAPI application that requires a
persistent runtime — it cannot run on Cloudflare Pages or Workers. However, you
can run it on **your own machine** (laptop, home server, VPS) and expose it
securely to the internet using **Cloudflare Tunnel** at zero hosting cost.

### How it works

Cloudflare Tunnel (`cloudflared`) creates an encrypted outbound connection from
your machine to Cloudflare's edge network. Traffic flows:

```
User → Cloudflare Edge (Access auth) → Tunnel → Your machine (localhost:8000)
```

Your machine never exposes any ports to the internet. No port forwarding, no
firewall rules, no static IP required. Cloudflare handles TLS, DDoS
protection, and (optionally) authentication via Access.

### Why this works well for a small private deployment

- **Zero hosting cost** — run on hardware you already own
- **Full capabilities** — Docker sandbox, filesystem, SQLite all work natively
- **Secure by default** — no inbound ports, encrypted tunnel, Access auth gate
- **Custom domain** — serve from `api.yourdomain.com` with automatic TLS
- **Same auth for everything** — dashboard and API behind the same Cloudflare
  Access policies

### Prerequisites

- A Cloudflare account (free)
- A domain added to Cloudflare (free tier works)
- `cloudflared` CLI installed on the machine running the API server

### Step 1 — Install cloudflared

```bash
# macOS
brew install cloudflared

# Debian/Ubuntu
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# Or download directly from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
```

### Step 2 — Authenticate cloudflared

```bash
cloudflared tunnel login
```

This opens a browser window. Select the domain you want to use (e.g.
`yourdomain.com`). A certificate is saved to `~/.cloudflared/cert.pem`.

### Step 3 — Create a tunnel

```bash
cloudflared tunnel create delegatron-api
```

This creates a tunnel and outputs a **tunnel ID** (a UUID). It also writes a
credentials file to `~/.cloudflared/<TUNNEL_ID>.json`.

### Step 4 — Configure DNS

Route a subdomain to your tunnel:

```bash
cloudflared tunnel route dns delegatron-api api.yourdomain.com
```

This creates a CNAME record pointing `api.yourdomain.com` to your tunnel.
Cloudflare handles TLS automatically.

### Step 5 — Create the tunnel config

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/your-user/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```

The `ingress` rules tell cloudflared to forward requests for
`api.yourdomain.com` to your local Delegatron server on port 8000. The final
catch-all rule returns 404 for anything else.

### Step 6 — Start the API server and tunnel

```bash
# Terminal 1: Start Delegatron API server
export DELEGATRON_AUTH_ENABLED=true
export DELEGATRON_STREAM_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
export DELEGATRON_CORS_ORIGINS=https://your-dashboard.example.com   # the dashboard's origin
export ANTHROPIC_API_KEY=sk-ant-...
delegatron serve --port 8000 --workspace /path/to/repos

# Terminal 2: Start the tunnel
cloudflared tunnel run delegatron-api
```

Your API is now live at `https://api.yourdomain.com`. With auth enabled the
server refuses to start unless `DELEGATRON_STREAM_SECRET` is set; keep it
stable across restarts (it signs the short-lived SSE/download URLs).

### Step 7 — Protect with Cloudflare Access (recommended)

Add an Access application for the API, the same way as the dashboard:

1. In **Zero Trust → Access → Applications → Add an application**
2. Choose **Self-hosted**
3. Set **Application domain** to `api.yourdomain.com`
4. Create the same **Allowed users** policy (or reuse the existing one)

For programmatic API access (CLI tools, CI/CD), create a **service token**:

1. Go to **Zero Trust → Access → Service Auth → Create Service Token**
2. Save the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers
3. Include both headers in API requests:

```bash
curl -H "CF-Access-Client-Id: <CLIENT_ID>" \
     -H "CF-Access-Client-Secret: <CLIENT_SECRET>" \
     -H "Authorization: Bearer dlg_your_key_here" \
     https://api.yourdomain.com/api/runs
```

### Step 8 — Run as a system service (optional)

For always-on operation, install cloudflared as a system service:

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

This ensures the tunnel reconnects automatically on reboot or network
interruption.

### Recommended small private deployment

```
┌─────────────────────────────────────────────────────────┐
│                   Cloudflare Edge                       │
│                                                         │
│  ┌─────────────────┐       ┌──────────────────────┐    │
│  │ Cloudflare Pages │       │  Cloudflare Access   │    │
│  │   (dashboard)    │       │  (auth gate for both) │    │
│  └────────┬────────┘       └──────────┬───────────┘    │
│           │                           │                 │
│           │              ┌────────────┴──────────┐     │
│           │              │  Cloudflare Tunnel     │     │
│           │              └────────────┬──────────┘     │
└───────────┼───────────────────────────┼─────────────────┘
            │                           │
            │                           │ (encrypted outbound)
            │                           │
┌───────────┼───────────────────────────┼─────────────────┐
│           │         Your Machine      │                  │
│           │                           │                  │
│    Browser loads              ┌───────┴────────┐        │
│    static assets              │ Delegatron API │        │
│    from Pages, then    ──►    │  localhost:8000 │        │
│    calls API via tunnel       │  + Docker sandbox│       │
│                               └────────────────┘        │
└─────────────────────────────────────────────────────────┘
```

- **Dashboard**: Cloudflare Pages (free) — static HTML/JS/CSS (needs a static export the dashboard no longer ships; see [Cloudflare Pages](#cloudflare-pages))
- **Auth**: Cloudflare Access (free, up to 50 users) — gates both dashboard and API
- **API server**: Your machine via Cloudflare Tunnel (free) — full Python runtime
- **Sandbox**: Docker running locally on the same machine
- **Total hosting cost**: $0

### Updating the dashboard API URL

When using this setup, build the dashboard with your tunnel domain (see the
note under [Cloudflare Pages](#cloudflare-pages): the build no longer produces
`out/`):

```bash
cd dashboard
NEXT_PUBLIC_API_URL=https://api.yourdomain.com npm run build
npx wrangler pages deploy out --project-name=delegatron
```

Or set `NEXT_PUBLIC_API_URL` in the Cloudflare Pages environment variables
if using GitHub-connected builds.

## API Server Hosting with Fly.io

[Fly.io](https://fly.io) is a platform that runs containers on servers close to
your users. It offers a free tier that works well for small private deployments
— no credit card required to start.

### Why Fly.io works for Delegatron

- **Free tier** — 3 shared-cpu-1x VMs with 256 MB RAM, 3 GB persistent storage
- **Persistent volumes** — SQLite database and run artifacts survive deploys
- **Always on** — no cold starts, no sleeping (unlike Heroku free tier)
- **Docker native** — deploys from a Dockerfile, full Python runtime
- **Global edge** — automatic TLS, custom domains, optional private networking

### Prerequisites

- A Fly.io account (free at [fly.io/app/sign-up](https://fly.io/app/sign-up))
- `flyctl` CLI installed

### Step 1 — Install flyctl

```bash
# macOS
brew install flyctl

# Linux
curl -L https://fly.io/install.sh | sh

# Or see https://fly.io/docs/flyctl/install/
```

### Step 2 — Authenticate

```bash
fly auth login
```

### Step 3 — Dockerfile

Use the repository's `Dockerfile` as is. It installs `git` and the
`cloud-providers` extra, and the server takes its paths from
`DELEGATRON_WORKSPACE`, `DELEGATRON_ARTIFACTS_DIR` and `DELEGATRON_DATA_DIR`.
The E2B sandbox used below also needs the `e2b` extra: deploy with
`fly deploy --build-arg EXTRAS=cloud-providers,e2b` (Step 7).

### Step 4 — Launch the app

```bash
fly launch --name delegatron-api --region sjc --no-deploy
```

Choose your preferred region (`sjc` = San Jose, `iad` = Virginia, `lhr` =
London, etc.). This creates a `fly.toml` configuration file.

### Step 5 — Create a persistent volume

Delegatron stores SQLite data and run artifacts on disk. Attach a volume so
data persists across deploys:

```bash
fly volumes create delegatron_data --size 1 --region sjc
```

Update `fly.toml` to mount the volume:

```toml
[mounts]
  source = "delegatron_data"
  destination = "/data"
```

Point the server's workspace, artifacts and database at the volume:

```bash
fly secrets set \
  DELEGATRON_WORKSPACE=/data/workspace \
  DELEGATRON_ARTIFACTS_DIR=/data/runs \
  DELEGATRON_DATA_DIR=/data/db
```

### Step 6 — Set secrets

```bash
fly secrets set \
  DELEGATRON_AUTH_ENABLED=true \
  DELEGATRON_STREAM_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  DELEGATRON_CORS_ORIGINS=https://your-dashboard.example.com \
  ANTHROPIC_API_KEY=sk-ant-... \
  OPENAI_API_KEY=sk-... \
  GEMINI_API_KEY=... \
  DELEGATRON_SANDBOX=e2b \
  E2B_API_KEY=e2b_...
```

> **Note:** Docker sandbox won't work on Fly.io (no Docker-in-Docker on free
> tier, and the image has no Docker CLI). Use the **E2B** sandbox instead, or
> run tests directly on the VM (`DELEGATRON_SANDBOX=local`, no isolation) if
> your tasks don't require sandboxing.

### Step 7 — Deploy

```bash
fly deploy --build-arg EXTRAS=cloud-providers,e2b
```

Your API server is now live at `https://delegatron-api.fly.dev`.

### Step 8 — Custom domain (optional)

```bash
fly certs add api.yourdomain.com
```

Then add a CNAME record in your DNS provider pointing `api.yourdomain.com` to
`delegatron-api.fly.dev`. Fly.io handles TLS automatically.

### Step 9 — Verify

```bash
# Health check
curl https://delegatron-api.fly.dev/api/health

# Create a run (with auth)
curl -X POST \
  -H "Authorization: Bearer dlg_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"request": "hello world", "preset": "budget"}' \
  https://delegatron-api.fly.dev/api/runs
```

### Monitoring and logs

```bash
# Stream live logs
fly logs

# Check app status
fly status

# SSH into the running VM
fly ssh console
```

### Recommended Fly.io + Cloudflare Pages architecture

```
┌────────────────────────────────────────────────────┐
│                   Cloudflare                        │
│                                                     │
│  ┌─────────────────┐     ┌──────────────────────┐  │
│  │ Cloudflare Pages │     │  Cloudflare Access   │  │
│  │   (dashboard)    │     │  (auth for dashboard) │  │
│  └────────┬────────┘     └──────────────────────┘  │
└───────────┼────────────────────────────────────────┘
            │
            │  API calls
            │
┌───────────┼────────────────────────────────────────┐
│           ▼          Fly.io                         │
│  ┌─────────────────────────────────┐               │
│  │    Delegatron API (Python)      │               │
│  │    delegatron-api.fly.dev       │               │
│  │    + Bearer token auth          │               │
│  └──────────────┬──────────────────┘               │
│                 │                                    │
│        ┌────────┴────────┐                          │
│        │ Persistent Vol  │                          │
│        │ /data (SQLite,  │                          │
│        │  artifacts)     │                          │
│        └─────────────────┘                          │
└────────────────────────────────────────────────────┘
```

- **Dashboard**: Cloudflare Pages (free)
- **Dashboard auth**: Cloudflare Access (free, up to 50 users)
- **API server**: Fly.io free tier
- **API auth**: Built-in Bearer token auth (`DELEGATRON_AUTH_ENABLED=true`)
- **Sandbox**: E2B (remote microVMs)
- **Total hosting cost**: $0 (within free tier limits)

## Authentication

Auth is **disabled by default** for ease of development. When disabled, all requests run as a default admin user.

### Enabling auth

```bash
export DELEGATRON_AUTH_ENABLED=true
export DELEGATRON_STREAM_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
```

With auth enabled the server refuses to start without `DELEGATRON_STREAM_SECRET`,
which signs the short-lived URLs that SSE streams and downloads use
(`GET /api/stream-token`).

### Creating API keys

The server has no key-management endpoint or CLI command; create keys with the
stores in `delegatron.server` against the server's database
(`$DELEGATRON_DATA_DIR/delegatron.db`). Keys use the `dlg_` prefix and are stored as SHA-256 hashes -- the raw key is shown only once at creation time.

```python
from delegatron.server.auth import APIKeyManager
from delegatron.server.db import init_db
from delegatron.server.teams import TeamStore

db = "data/delegatron.db"  # $DELEGATRON_DATA_DIR/delegatron.db
init_db(db)
team = TeamStore(db).create_team("ops", owner_id="alice", owner_email="alice@example.com")
raw_key, _ = APIKeyManager(db).create_key(user_id="alice", team_id=team.team_id)
print(raw_key)  # dlg_...
```

A key's role is its user's role in the key's team: `admin` (the team owner),
`member` or `viewer`.

### Using API keys

Include the key as a Bearer token:

```bash
curl -H "Authorization: Bearer dlg_your_key_here" \
  http://localhost:8000/api/runs
```

## Sandbox Configuration

Delegatron executes generated code in an isolated sandbox to prevent untrusted
code from affecting your host system. Two isolating backends are available
(`local`, alias `none`, runs commands on the host without isolation):

| Backend | Isolation | Latency | Cost | Best for |
|---|---|---|---|---|
| **Docker** (default) | Container-level | Low (~1s startup) | Free | Local dev, self-hosted, VPS |
| **E2B** | microVM (Firecracker) | Medium (~3-5s startup) | Pay-per-use | Cloud/PaaS deploys, stronger isolation |

### Using Docker sandbox (default)

Docker is the default sandbox backend. It runs generated code in ephemeral
containers on the same machine as the API server.

**Prerequisites:**

- Docker Engine 20.10+ installed and running
- The `docker` CLI on `PATH` (without it a run on the docker sandbox fails before it starts)
- The user running Delegatron must have access to the Docker socket

```bash
# Verify Docker is available
docker info

# Start Delegatron with Docker sandbox (this is the default)
export DELEGATRON_SANDBOX=docker
delegatron serve
```

**How it works (2-phase execution):**

1. The implementer agent produces a `PatchSet` with full file contents and/or search/replace edits
2. The orchestrator copies the workspace to a sandbox directory and applies the patch there
3. **Phase 1 (install)**: Dependencies are installed in a Docker container WITH network access (cached across iterations as a committed image)
4. **Phase 2 (test)**: Tests run in a Docker container WITHOUT network (`--network none`); containers are limited to 512 MB of memory and 1 CPU
5. Test output (pass/fail, stdout/stderr, coverage) is captured as a `TestReport`
6. The sandbox directory is cleaned up after execution

The 2-phase approach allows `pip install` to fetch packages while keeping test execution fully network-isolated.

**Limitations:**

- Requires Docker on the host — won't work on PaaS platforms without
  Docker-in-Docker support (e.g. Fly.io free tier, most serverless platforms)
- Container isolation is strong but not as hardened as a microVM

### Using E2B sandbox

[E2B](https://e2b.dev) runs code in Firecracker microVMs — the same technology
used by AWS Lambda and Fargate. Each execution gets a dedicated microVM that is
destroyed after use.

**Step 1 — Get an E2B API key**

1. Sign up at [e2b.dev](https://e2b.dev)
2. Go to your dashboard and copy your API key

**Step 2 — Configure Delegatron**

```bash
export DELEGATRON_SANDBOX=e2b
export E2B_API_KEY=e2b_your_key_here
```

E2B also needs the `e2b` extra: `pip install -e ".[e2b]"` (or build the image
with `EXTRAS=cloud-providers,e2b`).

Or in your `delegatron.toml`:

```toml
[pipeline]
sandbox = "e2b"
```

**Step 3 — Verify**

```bash
# Start the server
delegatron serve

# Create a test run — it should execute in an E2B microVM
curl -X POST \
  -H "Authorization: Bearer dlg_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"request": "create a hello world python script", "preset": "budget"}' \
  http://localhost:8000/api/runs
```

Check the server log (`logs/server.log`, or under `DELEGATRON_LOG_DIR`) for `Using E2B sandbox` to confirm the correct backend is in use.

**When to use E2B:**

- Deploying on **Fly.io**, **Railway**, **Render**, or other PaaS without Docker
- You want **stronger isolation** than containers (microVM boundary)
- You don't want to manage Docker on your host
- You need **multi-language support** out of the box (E2B templates)

**E2B pricing:**

E2B offers a free tier with limited compute hours. Check
[e2b.dev/pricing](https://e2b.dev/pricing) for current limits. For private
deployment with low run volume, the free tier is typically sufficient.

### Switching between sandboxes

Switching is a single environment variable change — no code changes or
restarts required beyond restarting the server:

```bash
# Switch to E2B
export DELEGATRON_SANDBOX=e2b
export E2B_API_KEY=e2b_...

# Switch back to Docker
export DELEGATRON_SANDBOX=docker
unset E2B_API_KEY  # optional, just ignored when not using E2B
```

The sandbox backend defaults to the server-wide setting, but can be overridden per-run (`docker` or `e2b`) via the `sandbox` field in the `POST /api/runs` request or the New Run page in the dashboard.

### Which sandbox for which deploy target?

| Deploy target | Recommended sandbox | Why |
|---|---|---|
| Local machine | Docker | Zero cost, low latency, full control |
| VPS / bare metal | Docker | Same as local, direct Docker access |
| Cloudflare Tunnel (your machine) | Docker | Running on your own hardware |
| Fly.io free tier | E2B | No Docker-in-Docker support |
| Railway / Render | E2B | No Docker-in-Docker support |
| Docker Compose (shipped image) | E2B | The image has no Docker CLI (or see [Sandbox](#sandbox) for host Docker) |
| Kubernetes (Helm chart) | E2B | The image has no Docker CLI and the chart mounts no Docker socket |

## Environment Variables Reference

### LLM Provider Keys

| Variable | Description | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | If using Anthropic models |
| `OPENAI_API_KEY` | OpenAI API key (sent only to the OpenAI API; other endpoints need `api_key_env`) | If using OpenAI models |
| `GEMINI_API_KEY` | Google Gemini API key | If using Google models |

### Pipeline Configuration

| Variable | Description | Default |
|---|---|---|
| `DELEGATRON_MAX_ITERATIONS` | Max review loop iterations per task | `5` |
| `DELEGATRON_MAX_COST_USD` | Max spend in USD per run (`0` = no limit) | `5.0` |
| `DELEGATRON_MAX_WALL_TIME` | Max wall-clock time in minutes per run | `60` |
| `DELEGATRON_SANDBOX` | Sandbox backend: `docker`, `e2b`, `local`, or `none` | `docker` |
| `DELEGATRON_LOG_LEVEL` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `DELEGATRON_BASELINE_TESTS` | Run the workspace's tests once before the first patch (`[pipeline] baseline_tests`) | `true` |
| `DELEGATRON_PARALLEL_TASKS` | Generate first patches of ready tasks in parallel (`[pipeline] parallel_tasks`) | `false` |
| `DELEGATRON_PROJECT_MODE` | Enable project mode (the server's `/api/projects` endpoints) | `false` |
| `DELEGATRON_CREATE_PR` | Open a GitHub PR when a run is approved (needs an authenticated `gh` CLI) | `false` |
| `DELEGATRON_PR_BASE` | Base branch for that PR | `main` |
| `DELEGATRON_PR_DRAFT` | Open that PR as a draft | `false` |
| `DELEGATRON_LOCAL_HOSTS` | Extra host names the zero-cloud report counts as local (comma-separated; `[pipeline] local_hosts`) | (none) |
| `GH_HOST` | `gh`'s GitHub Enterprise host; the zero-cloud report records `create_pr` API calls to it | `github.com` |
| `OLLAMA_CONTEXT_LENGTH` | Context window assumed for an Ollama model when neither its Modelfile nor the loaded model reports one | Ollama's `4096` |

### Model Overrides

| Variable | Description | Default |
|---|---|---|
| `DELEGATRON_ARCHITECT_MODEL` | Architect model when neither a preset nor `delegatron.toml` sets one | `claude-sonnet-5-5` |
| `DELEGATRON_IMPLEMENTER_MODEL` | Implementer model when neither a preset nor `delegatron.toml` sets one | `gpt-4.1` |
| `DELEGATRON_REVIEWER_MODEL` | Reviewer model when neither a preset nor `delegatron.toml` sets one | `gemini-2.5-pro` |

### Server / Auth

| Variable | Description | Default |
|---|---|---|
| `DELEGATRON_AUTH_ENABLED` | Enable API key authentication (`true`/`false`) | `false` |
| `DELEGATRON_STREAM_SECRET` | Secret that signs short-lived SSE/download URLs; required when auth is enabled | random per process (auth off) |
| `DELEGATRON_CORS_ORIGINS` | Browser origins allowed to call the API, e.g. the dashboard's (comma-separated) | `http://localhost:3000,http://localhost:8001,http://localhost:8002,http://localhost:8081` |
| `DELEGATRON_APPROVAL_TIMEOUT_MINUTES` | How long an interactive run waits for plan approval before rejecting it | `30` |
| `DELEGATRON_DATA_DIR` | Directory for API keys and team data | `data` |
| `DELEGATRON_WORKSPACE` | Server workspace when serving with uvicorn directly (`delegatron serve` uses `--workspace`) | current directory |
| `DELEGATRON_ARTIFACTS_DIR` | Run artifacts directory (used by `delegatron serve`, or with `DELEGATRON_WORKSPACE`; otherwise `./runs`) | `<install dir>/runs` |
| `DELEGATRON_WORKSPACE_ROOT` | Local `workspace` paths in API requests must lie under this directory | (any) |
| `DELEGATRON_GIT_ALLOWED_HOSTS` | Hosts API requests may name in a git-URL `workspace` (comma-separated) | `github.com,gitlab.com,bitbucket.org` |
| `DELEGATRON_ALLOWED_BASE_URLS` | Extra `base_url`s API role overrides may use with the server's API keys (comma-separated) | (none) |
| `DELEGATRON_ALLOWED_KEY_ENVS` | Extra env var names API role overrides may give as `api_key_env` (comma-separated) | (none) |
| `DELEGATRON_WEBHOOK_SECRET` | GitHub webhook signature secret | (none) |
| `DELEGATRON_ALERT_WEBHOOK_URL` | POST webhook when a run ends `error`, `exhausted`, `stalled` or `partial`, or is cancelled | (none) |
| `DELEGATRON_DOCKER_IMAGE` | Custom Docker image for sandbox | auto-detect |
| `OLLAMA_BASE_URL` | Ollama instance that `/api/health/full`, `/api/system/stats` and `/api/defaults` query (not the presets' model endpoint) | `http://localhost:11434` |
| `DELEGATRON_ELECTRICITY_RATE` | Electricity rate in $/kWh for local GPU cost | `0.12` |
| `DELEGATRON_GPU_TDP_WATTS` | Fallback GPU power draw (watts) when nvidia-smi unavailable | `250` |
| `DELEGATRON_GPU_PRICE_USD` | GPU purchase price for hardware amortization | `1500` |
| `DELEGATRON_GPU_LIFETIME_HOURS` | Expected GPU lifetime in hours for amortization | `20000` |

### Logging

| Variable | Description | Default |
|---|---|---|
| `DELEGATRON_LOG_DIR` | Directory for `server.log` and the CLI's `delegatron.log` | `<install dir>/logs` |
| `DELEGATRON_LOG_ROTATE` | Rotation: `time` (daily) or `size` | `time` |
| `DELEGATRON_LOG_ROTATE_DAYS` | Daily logs kept with `time` rotation | `7` |
| `DELEGATRON_LOG_MAX_MB` | File size that triggers `size` rotation (5 backups kept) | `100` |
| `DELEGATRON_LOG_TIMEZONE` | Log timestamps: `utc` or `local` | `utc` |

### Sandbox

| Variable | Description | Required? |
|---|---|---|
| `E2B_API_KEY` | E2B API key for microVM sandbox | Only if `DELEGATRON_SANDBOX=e2b` |

## Security Considerations

### API Keys

- Keys are stored as SHA-256 hashes -- the raw key is only shown once at creation
- Keys use the `dlg_` prefix for easy identification in secret scanners
- Rotate keys regularly: create a new key and revoke the old one with
  `APIKeyManager.revoke_key` (there is no key-management endpoint)
- With auth disabled (the default) every request runs as an admin and the
  server logs an `INSECURE DEV MODE` warning at startup; enable auth before
  exposing it to a network

### Request-supplied workspaces and endpoints

- A git-URL `workspace` must be https, ssh or `user@host:path` on a host in
  `DELEGATRON_GIT_ALLOWED_HOSTS`; option-like values are rejected before git runs.
  Set `DELEGATRON_WORKSPACE_ROOT` to confine local paths.
- A role override's `base_url` gets none of the server's API keys unless it is
  one of the operator's configured role `base_url`s or listed in
  `DELEGATRON_ALLOWED_BASE_URLS`; `api_key_env` is limited to the provider's
  default variable, the variable configured for that role, and
  `DELEGATRON_ALLOWED_KEY_ENVS`.

### Network

- Always run behind TLS (HTTPS) in production
- Use network policies in Kubernetes to restrict pod-to-pod traffic
- The API server should not be exposed directly -- use an ingress controller or load balancer

### Secrets

- Never commit API keys to version control
- Use Kubernetes secrets or environment variables
- The built-in secrets scanner flags accidental key exposure in generated code
  (reported by default; set `[pipeline] security_mode = "block"` to block approval)

### Sandboxing

- Docker sandbox runs generated code in isolated containers (dependency installs
  have network access; tests run with `--network none`)
- E2B sandbox uses microVMs for stronger isolation
- Never run untrusted code outside a sandbox

## Upgrading

### Docker Compose

```bash
git pull
docker compose build
docker compose up -d
```

### Helm

```bash
# after building and pushing the new image
helm upgrade delegatron ./helm --set image.tag=<new-version>
```

### Rolling back

```bash
# Helm
helm rollback delegatron <revision>

# Docker Compose (the image is built from the checkout)
docker compose down
git checkout <previous-release>
docker compose build
docker compose up -d
```
