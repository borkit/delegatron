# ADR-001: Pipeline Architecture Overview

## Status
Accepted

## Context
Delegatron needs an architecture that orchestrates three LLM providers (Claude, GPT, Gemini) in an iterative coding loop with safe code execution and structured communication.

## Decision
We adopt a **hub-spoke state machine** architecture:

- **Orchestrator (hub)** owns all state, controls routing, and is the only component that mutates the repo or runs code
- **Agents (spokes)** are stateless advisors that receive structured input and return structured artifacts
- **LangGraph** provides the state machine framework with native cycle support
- **Structured JSON schemas** (not free-form chat) for all inter-agent communication

> **Note (2026-10):** the loop currently runs as a plain async loop in `orchestrator/graph.py` (`Pipeline.execute`) that keeps the graph's routing shape; LangGraph is not a dependency yet.

### Agent Roles
| Agent | Provider | Role | Output |
|---|---|---|---|
| Architect | Claude | Plan, decompose | WorkPlan |
| Implementer | GPT | Code generation | PatchSet |
| Reviewer | Gemini | Code review | ReviewReport |
| Director | Architect's model | Rule on a conflict that recurs on a task | ArbitrationReport |

### Main Pipeline Flow

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                        User Request                             │
  │                             │                                   │
  │                    (--auto flag set?)                            │
  │                       ┌─────┴─────┐                             │
  │                      yes          no                            │
  │                       │            │                             │
  │                       ▼            │                             │
  │              ┌─────────────────┐   │                             │
  │              │  Auto-Select    │   │  Analyse request keywords,  │
  │              │  Preset         │   │  project languages/size     │
  │              └────────┬────────┘   │                             │
  │                       └─────┬──────┘                             │
  │                             ▼                                    │
  │                     Load Config / Preset                         │
  │                             │                                    │
  │                             ▼                                    │
  │                    ┌─────────────────┐                           │
  │                    │   Architect     │ ◄─── Produces WorkPlan    │
  │                    └────────┬────────┘                           │
  │                             │                                    │
  │                   (interactive mode?)                            │
  │                       ┌─────┴─────┐                             │
  │                      yes          no                            │
  │                       │            │                             │
  │                       ▼            │                             │
  │                 [User approves?]   │                             │
  │                   │          │     │                             │
  │                  no         yes    │                             │
  │                   │          │     │                             │
  │                   ▼          └──┬──┘                             │
  │                 STOP            │                                │
  │                                 ▼                                │
  │                    Baseline test run (tests that                 │
  │                    already fail are pre-existing)                │
  │                                 │                                │
  │                                 ▼                                │
  │  ┌─ ─ ─ ─ ─ ─ ─  Iteration Loop  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐    │
  │                                                             │    │
  │  │  Check termination (iterations / cost / wall-time)       │    │
  │                         │                                   │    │
  │  │                      ▼                                   │    │
  │     ┌─────────────────────────────┐                         │    │
  │  │  │  Context Gathering          │  Extract file refs,     │    │
  │     │  (context.py)               │  imports, siblings;     │    │
  │  │  │                             │  cap at 50 KB           │    │
  │     └────────────┬────────────────┘                         │    │
  │  │               ▼                                          │    │
  │     ┌─────────────────────────────┐                         │    │
  │  │  │  Implementer                │  Generates PatchSet     │    │
  │     │  (streaming tokens ──► CLI) │  ◄── via _invoke_agent  │    │
  │  │  └────────────┬────────────────┘                         │    │
  │                  │                                          │    │
  │  │               ├── Constraint validation                  │    │
  │                  ├── Secrets scanning                       │    │
  │  │               ├── Dependency audit                       │    │
  │                  ├── Apply patch to git workspace           │    │
  │  │               ▼                                          │    │
  │     ┌─────────────────────────────┐                         │    │
  │  │  │  Sandbox (Docker/E2B/local) │  Runs tests, returns    │    │
  │     │                             │  TestReport             │    │
  │  │  └────────────┬────────────────┘                         │    │
  │                  ▼                                          │    │
  │  │  ┌─────────────────────────────┐                         │    │
  │     │  Reviewer                   │  Produces ReviewReport  │    │
  │  │  │  (streaming tokens ──► CLI) │                         │    │
  │     └────────────┬────────────────┘                         │    │
  │  │               │                                          │    │
  │                  ├── EMCS scoring (may override to accept)  │    │
  │  │               ├── Arbitration: a conflict that recurs    │    │
  │                  │   on the task (same test failure or      │    │
  │  │               │   same review issues) goes once to the   │    │
  │                  │   director; its ruling (ACCEPT / REVISE  │    │
  │  │               │   / DEFER) updates the review, and       │    │
  │                  │   REVISE gives one directed attempt      │    │
  │  │               ▼                                          │    │
  │     ┌─────────────────────────────┐                         │    │
  │  │  │  Completion gate / stall    │                         │    │
  │     │  policy / router (per task) │                         │    │
  │  │  │  passed  ──► next task      │  last task: DONE        │    │
  │     │  stalled ──► skip task      │  also at max_iterations │    │
  │  │  │  "architect" ──► re-plan    │                         │    │
  │     │  "implementer" ──► iterate  │                         │    │
  │  │  │  budget hit ──► DONE        │  cost, time, iterations │    │
  │     └─────────────────────────────┘                         │    │
  │  │                                                          │    │
  │  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘    │
  └─────────────────────────────────────────────────────────────────┘
```

### Plan-Only Flow

```
  Request ──► Load Config ──► Architect ──► WorkPlan (returned immediately)
                                              │
                                         No execution.
                                         No sandbox, no
                                         implementer, no
                                         reviewer.
```

### Refine Flow (Interactive Iteration)

```
  Load previous run artifacts
           │
           ▼
  Display current state (plan, code, tests, review)
           │
           ▼
  ┌──── Prompt user for feedback ◄───────────────┐
  │        │                                      │
  │   "done" / Ctrl-C                             │
  │        │                        Display updated results
  │        ▼                                      │
  │      STOP                                     │
  │                                               │
  └──► Inject feedback as review issues           │
           │                                      │
           ▼                                      │
       Implementer (with feedback context)        │
           │                                      │
           ▼                                      │
       Sandbox (run tests)                        │
           │                                      │
           ▼                                      │
       Reviewer (evaluate changes)                │
           │  (repeats until approved or the      │
           │   round's limits are reached)        │
           ▼                                      │
       Save checkpoint ───────────────────────────┘
```

### Benchmark Flow

```
  Request + Presets[A, B, C, ...]
           │
           ▼
  ┌────────────────────────────────────┐
  │  For each preset (sequentially):   │
  │                                    │
  │    Load config for preset          │
  │         │                          │
  │         ▼                          │
  │    Full pipeline run               │
  │    (architect ──► impl ──► test    │
  │     ──► review ──► route loop)     │
  │         │                          │
  │         ▼                          │
  │    Record: status, EMCS score,     │
  │    iterations, cost, elapsed time  │
  │                                    │
  └────────────────────────────────────┘
           │
           ▼
  Compare all results
           │
           ▼
  Report: best quality, cheapest, fastest
```

### Auto-Select Preset (Pre-Pipeline)

When `--auto` is passed on the CLI (or an API run request sets `"preset": "auto"`), the auto-selection module (`autoselect.py`) analyses the task request text and project characteristics to choose the best preset before the pipeline starts. It scores the request on keyword signals (simple vs. complex), target language difficulty, and project file count, then maps the aggregate score to `budget`, `default`, or `claude-heavy`. This lets users who are unsure which preset to choose get reasonable defaults without manual configuration.

### Streaming Mode

The pipeline supports a `stream=True` execution mode that propagates tokens in real time without changing the pipeline's control flow:

1. Each agent call uses `stream_invoke` (on `BaseAgent`) instead of `invoke`
2. `stream_invoke` delegates to the provider's `stream_generate` method, which yields `str` token chunks followed by a final `ProviderResponse`
3. The orchestrator's `_invoke_agent` helper relays each token chunk through the `step_callback` as a `token` event with the originating role
4. The CLI (`--stream` flag) prints tokens to stderr; the server broadcasts them as SSE events to connected dashboard clients
5. If the streamed output fails schema validation, the system transparently falls back to the non-streaming `invoke` retry path with error feedback injected

The Anthropic and OpenAI adapters (the latter also serving OpenAI-compatible endpoints such as Ollama) implement true `stream_generate`; the Google and mock providers use the base class's single-chunk fallback. A stream that breaks off with a retryable error is retried without streaming.

### Multi-File Context Gathering

Before each implementer call, the orchestrator invokes `select_context()` (`context.py`) to select relevant file contents from the workspace, in this priority order:

1. Files listed in the current task's `outputs` field
2. Files of the failing tests, each followed by the local modules it imports
3. Files cited by reviewer feedback (`file`, `fix_file`, `evidence` and `recommended_fix`), each followed by the local modules it imports
4. Files written by earlier tasks
5. Test ↔ source counterparts (`test_x.py`, `x_test.py`, root-level and `src/` layouts) and package `__init__.py` files
6. Files the task description names
7. Their counterparts, sibling source files in the same directories, local modules imported by the files above or named in the task description

A structural repo map (class/function signatures) is passed alongside the files. Total context is capped at 50KB (each file at most 50KB); when the implementer's context window is known (configured `context_window`, or detected from Ollama) the cap shrinks to 60% of the input tokens left beside the system prompt, schema and response reserve (at least 2KB). Files of tiers 1–4 and 6 that no longer fit are shown as a head + tail excerpt; others are left out and listed by name.

### Post-Pipeline Refinement Loop

After a pipeline run completes, `delegatron refine <run_id>` enters an interactive conversation loop. It loads the saved state and artifacts from the previous run, displays the current plan/code/tests/review, then prompts the user for feedback. Each round re-opens the run's current task, injects the feedback as a synthetic review issue (and appends it to the request), and runs the implementer → sandbox → reviewer loop on that task again with a fresh `max_iterations` budget for the round. The user can repeat this until satisfied, then type `done` to exit. The refined state is checkpointed after each round.

### Prompts for Local Models

The `prompts/` directory holds v1 (cloud models) and v2 to v5 (local models) for the architect, implementer and reviewer, plus `director/v1.md`, `meta_architect/v1.md` (milestones for `delegatron project`) and the retry notes `validation_retry/v1.md` and `truncation_retry/v1.md` that an agent appends to its user message after invalid or cut-off output.

**v2/v3** (legacy):
- Earlier iterations optimized for smaller local models and extended iteration runs.

**v4** (previous local default, kept unchanged as the A/B baseline):
- Pin `prompt_version = 4` on a role to compare it with v5
- Still the architect prompt of the `single-claude` and `single-gpt` presets (their implementer and reviewer use v1)

**v5** (used by `local`, `local-phi4`, `local-heavy`, `local-oss`, `local-oss-review`):
- Pinned for all three roles by every local preset
- Fix cycles rank the evidence: failing tests first, reviewer notes as advice, and only one side of a failing assertion changes per attempt
- Structured JSON output constraints and sandbox environment warnings (no network access in test containers, mock all HTTP calls)
- A role whose `prompt_version` is 0 (unset) gets v5 on a local endpoint or the mock provider and v1 on a cloud API (`factories/agent_factory.resolve_prompt_version`)

### Review Flow (Read-Only)

```
  Request: "delegatron review" or POST /api/review
           │
           ├── (--logs provided?)
           │    yes ──► Parse log content ──► Reviewer (log analysis mode)
           │    no
           │    │
           ▼    │
  Resolve workspace (local path or git clone)
           │
           ▼
  Detect project info (languages, frameworks, test commands)
           │
           ▼
  Read the top-ranked source files (within the context budget) + repo map
           │
           ▼
  Reviewer agent (structured ReviewReport)
           │
           ├── (--with-suggestions?)
           │    yes ──► Architect agent (improvement plan)
           │    no
           │
           ▼
  Return ReviewReport (+ suggestions if requested)

  No code generated. No sandbox. No implementer. Read-only audit.
```

### Log Analysis Flow

```
  Log content (from --logs file or dashboard paste)
           │
           ▼
  Reviewer agent in log analysis mode
           │
           ▼
  Structured report:
    - Errors identified with root causes
    - Patterns detected (recurring failures, timing)
    - Actionable fixes with evidence
    - Severity classification
```

### Termination
Tasks run in plan order. A task completes when it passes the completion gate (reviewer approves under the hard gates — patch applied, real tests pass, no blocker/major issue — or EMCS overrides a minor-only rejection, and no accepted test of an earlier task went missing). A task that stalls or uses up its `max_iterations` is skipped and the run moves on. The run ends with status:
- `approved` — every task completed
- `partial` — some tasks completed, others were skipped (or an approved task's accepted test broke later)
- `exhausted` — the cost budget, the wall time or the run's iteration cap (`max_iterations` per planned task) ran out, or the last pending task used up its own `max_iterations`
- `stalled` — the last pending task stalled: two no-change patches in a row, or, after 3 attempts (5 with a local implementer), the same test failure twice or (without failing tests) 70%+ of the review issues repeated
- `error` — the implementer or reviewer failed twice in a row on a task, the first plan failed or had no tasks, the implementer reported it is blocked, the plan was rejected (`--interactive`), or the workspace or sandbox could not be prepared

## Consequences
- Models are interchangeable (swap providers without changing orchestration)
- Full auditability via event log
- Predictable cost via budget guardrails
- Slightly more complex than a simple chat loop, but far more reliable
