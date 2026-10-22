# Delegatron Dashboard

A Next.js web dashboard for monitoring and managing Delegatron pipeline runs. `delegatron serve` starts it with `next start` on the port after the API's (`8001` by default) once it is built.

## Tech Stack

- **Framework:** Next.js 16 (App Router)
- **UI:** React 19, Tailwind CSS 4
- **Streaming:** Server-Sent Events (SSE) for live pipeline events

## Setup

```bash
cd dashboard
npm install
```

## Development

```bash
npm run dev
```

The dashboard runs at http://localhost:3000 by default.

Run the unit tests (`src/lib/*.test.ts`) with `npm test` (Node's built-in test runner; Node 23.6 or later runs the TypeScript files directly).

## Configuration

Set the Delegatron API URL via environment variable:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Or create a `.env.local` file:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
```

`.env.development` and `.env.production` already set `http://localhost:8000`. Next.js builds `NEXT_PUBLIC_API_URL` into the client, so change it before `npm run build`. The API URL can also be changed at runtime from the Settings page (saved in the browser); it takes precedence. Without either, the dashboard calls the same host on the port before its own.

When the server has `DELEGATRON_AUTH_ENABLED=true`, store an API key under Settings > Authentication; the dashboard sends it as a Bearer token and fetches signed URLs (`/api/stream-token`) for event streams.

## Pages

| Page | Route | Description |
|------|-------|-------------|
| **Home** | `/` | Overview with system health widget (Server, Ollama, Docker, Disk status) |
| **New Run** | `/new` | Task description, preset selection (including Auto), workspace path or git URL, sandbox type (Docker or E2B), Docker image dropdown, limits, interactive mode, role overrides, constraints, cost estimate, Plan Only preview, Save as Template |
| **Runs** | `/runs` | Browse runs with checkbox selection, select all, bulk delete |
| **Run Detail** | `/runs/[id]` | Live SSE events, planning progress bar, Live Thinking panel (streamed model output), iteration history timeline with diff viewer and restore, live system stats with sparklines (Ollama VRAM, running Docker containers with per-container CPU/memory), code viewer (tabbed files), sandbox info panel, local GPU cost breakdown, plan approval, Retry (a new run: fresh, or fixing the review issues, optionally with another preset) and Resume (continue the same run from its last checkpoint; an exhausted run asks for a higher iteration limit) |
| **Review** | `/review` | Read-only code review and log file analysis |
| **Compare** | `/compare` | Side-by-side metrics table for any two runs |
| **Compliance** | `/compliance` | Zero-cloud verification with verdict, event log, fingerprint, JSON export |
| **Templates** | `/templates` | 30 built-in + custom templates with variable substitution; a template opens pre-filled in New Run |
| **Analytics** | `/analytics` | Total runs, spend, average cost per run, success rate, runs by status, preset performance table |
| **Projects** | `/projects` | Multi-milestone project mode (enable it in Settings > Experimental Features first) |
| **Settings** | `/settings` | API endpoint, default preset, pipeline defaults (kept in the browser only where they differ from the server's `/api/defaults`), log level and timezone, Ollama restart, experimental features (parallel tasks, project mode), alerting, API key status, authentication key, settings export/import |

## Features

- **Dark theme** (the light theme was removed)
- **Active run indicator** in sidebar (clickable, auto-updating)
- **Toast notifications** for success/error/info events
- **Planning progress bar** with step labels and auto-scroll
- **Sparkline charts** for Ollama VRAM and running Docker containers (with per-container CPU/memory)
- **Code viewer** with tabbed files and language hints
- **Sandbox info panel** showing image, OS, shell, timing, network isolation
- **Preset performance table** with approval rate and error rate per preset
- **Double-click prevention** on action buttons (Retry, Resume)
- **Tooltips** on all interactive elements (30+ buttons, badges, and controls)
- **Live Thinking panel** for the models' streamed output during active runs
- **Iteration History** timeline with per-iteration diff viewer and checkpoint restore
- **Local GPU cost breakdown** showing electricity, hardware wear, and GPU time for Ollama models

## Build

```bash
npm run build
npm start
```

`npm run build` produces a Next.js production build in `.next/`; `npm start` (`next start`) serves it. `delegatron serve` does the same on the port after the API's whenever `dashboard/.next` exists. Next.js 16 needs Node.js 20.9 or later.

### Cloudflare Pages

The dashboard is no longer a static export: `next.config.js` does not set `output: "export"`, so `npm run build` writes no `out/` directory, although `wrangler.toml` still names `out` as the Pages output directory. Deploying to Cloudflare Pages as a static site needs that export setting restored.

## Windows Quick Start

Use the one-click launcher:

```
start-dashboard.bat
```

It needs the virtual environment from `setup.bat`, builds the dashboard if `dashboard/.next` is missing, and runs `delegatron serve`: the API on http://localhost:8000 and the dashboard on http://localhost:8001. It opens the browser, and Ctrl+C stops both.
