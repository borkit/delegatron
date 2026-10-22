# Delegatron System Architecture

> Comprehensive architectural reference for the Delegatron multi-agent coding pipeline.
> StoriaBrevi LLC — April 2026

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Core Pipeline: Plan / Code / Test / Review Loop](#2-core-pipeline)
3. [Agent Layer](#3-agent-layer)
4. [Provider Abstraction](#4-provider-abstraction)
5. [Orchestrator](#5-orchestrator)
6. [Sandbox Execution](#6-sandbox-execution)
7. [Schema Contract](#7-schema-contract)
8. [Server Layer (API)](#8-server-layer)
9. [Authentication and Multi-Tenancy](#9-authentication-and-multi-tenancy)
10. [Security Subsystem](#10-security-subsystem)
11. [Supporting Subsystems](#11-supporting-subsystems)
12. [CLI](#12-cli)
13. [Dashboard](#13-dashboard)
14. [Deployment](#14-deployment)
15. [Key Design Decisions](#15-key-design-decisions)
16. [Known Limitations and Future Work](#16-known-limitations)

---

## 1. System Overview

Delegatron is a multi-agent coding pipeline that orchestrates LLMs through a state-machine loop to produce tested, reviewed code from natural-language requests. It is **provider-agnostic** — any LLM (Anthropic Claude, OpenAI GPT, Google Gemini, or any OpenAI-compatible endpoint) can fill any agent role.

### High-Level Architecture

```
                         User Request
                              │
                              ▼
                    ┌─────────────────┐
                    │   CLI / API /   │
                    │   Dashboard     │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Orchestrator   │  ◄── Owns all state
                    │  (Pipeline)     │      Controls routing
                    └──┬──┬──┬──┬────┘      Enforces budgets
                       │  │  │  │
            ┌──────────┘  │  │  └──────────┐
            ▼             ▼  ▼             ▼
      ┌──────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐
      │Architect │  │Implement│  │ Sandbox  │  │Reviewer │
      │ (Claude) │  │ (GPT)   │  │(Docker/  │  │(Gemini) │
      │          │  │         │  │ E2B)     │  │         │
      └──────────┘  └─────────┘  └──────────┘  └─────────┘
            │             │            │              │
            ▼             ▼            ▼              ▼
        WorkPlan      PatchSet    TestReport    ReviewReport
```

A fifth agent, the **director** (`agents/director.py`), runs on the architect's model only when a conflict recurs on a task and returns an `ArbitrationReport` (see [Arbitration](#arbitration-orchestratorarbitrationpy)).

### Foundational Principles

1. **Provider-agnostic**: Any LLM in any role via adapter pattern
2. **Orchestrator owns all state**: Models are advisory; only the orchestrator mutates repos or runs code
3. **Evidence hierarchy**: Failing tests > security checks > reviewer blockers > director opinions
4. **Stateless agents**: Context is reset each iteration; no accumulated conversation history
5. **Idempotent operations**: Every step is safely retryable
6. **Budget guardrails always active**: `max_iterations`, `max_cost_usd`, `max_wall_time` enforced
7. **External prompts**: System prompts loaded from versioned files, never hardcoded

---

## 2. Core Pipeline

The pipeline implements a **plan → code → test → review → route** loop:

```
                       User Request
                            │
                   (--auto flag set?)──── yes ──► Auto-Select Preset
                            │                         │
                            no                        │
                            │◄────────────────────────┘
                            ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │   start ──► architect ──┬──(interactive?)──► user approve/reject │
  │                         │                          │             │
  │                         │◄──── approved ───────────┘             │
  │                         │                                        │
  │                         ▼                                        │
  │              ┌─── Iteration Loop ──────────────────────────┐     │
  │              │                                             │     │
  │              │  context gathering ──► implementer ──►      │     │
  │              │     (read files,         │                  │     │
  │              │      imports,            ├─ constraints     │     │
  │              │      siblings,           ├─ secrets scan    │     │
  │              │      cap 50KB)           ├─ dep audit       │     │
  │              │          ▲               ├─ apply to git    │     │
  │              │          │               ▼                  │     │
  │              │          │           sandbox ──►            │     │
  │              │          │           reviewer ──►           │     │
  │              │          │              │                   │     │
  │              │          │          ┌───┼──────────────┐    │     │
  │              │          │          │   │  EMCS score  │    │     │
  │              │          │          │   │  Arbitration │    │     │
  │              │          │          │   ▼              │    │     │
  │              │          │          │ router           │    │     │
  │              │          │          │   │              │    │     │
  │              │          │   ┌──────┼───┼──────────┐   │    │     │
  │              │          │   │      │   │          │   │    │     │
  │              │          │ approved │ architect  impl  │    │     │
  │              │          │   │      │   │          │   │    │     │
  │              │          │   ▼      │   │          │   │    │     │
  │              │          │ DONE     │   ▼          └───┘──► │     │
  │              │          │          │ re-plan ──►            │     │
  │              │          │          │ architect ──► ...      │     │
  │              │          │          └────────────────────    │     │
  │              └─────────────────────────────────────────────┘     │
  │                                                                  │
  │   Streaming path: _invoke_agent emits token-level callbacks      │
  │   via step_callback ──► CLI stderr / SSE events to client        │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

### Step-by-Step Flow

| Step | Component | Input | Output | Side Effects |
|------|-----------|-------|--------|-------------|
| 1 | **Architect** | User request + project info + file tree | `WorkPlan` | Plan saved to artifacts |
| 1b | **Interactive approval** (optional) | WorkPlan displayed to user | approve/reject | Pipeline stops if rejected |
| 1c | **Baseline tests** (`baseline_tests`, default on) | Workspace before any patch | Baseline `TestReport` | Tests that already fail are recorded as pre-existing, not blamed on the run |
| 2 | **Implementer** | TaskSpec + plan summary + selected files + repo map + test failures + review feedback | `PatchSet` | Patch saved; applied (and committed on the run branch) after the checks below |
| 2b | **Constraint validation** | Lines the patch adds + project constraints | Violations list | Violations logged as events; block the patch in `security_mode = "block"` |
| 2c | **Secrets scanning** | Lines the patch adds (vs. the run's baseline) | SecretFindings | Findings shown to the agents; block the patch and approval only in `security_mode = "block"` (default `warn`) |
| 2d | **Dependency audit** | Lines the patch adds + project deps | DepFindings | New deps flagged for review (never blocking) |
| 2e | **Import validation** | Applied files + package manifests | Import issues | Advisory: passed to the reviewer, and to the next implementer after a failed test run |
| 3 | **Sandbox** | PatchSet (whole files, with search/replace `edits` folded in; `delete_files`; or a diff) | `TestReport` | Code executed in Docker/E2B isolation (or on the host with the local sandbox) |
| 4 | **Reviewer** | Diff + TestReport + acceptance criteria | `ReviewReport` | Review saved to artifacts |
| 4b | **EMCS scoring** | ReviewReport + TestReport | Quality score 0.0–1.0 | Can override a minor/nit-only rejection with real passing tests if score >= threshold |
| 4c | **Arbitration** | Current review, failure signatures of the task | Conflict list | Director rules once per recurring conflict; REVISE gives one directed attempt |
| 5 | **Completion gate / stall policy / router** | Full state | Next task, task skipped, or direction: `approved` / `architect` / `implementer` | One gate for every task; stall policy per task (see below) |

### Termination Conditions

A multi-task plan runs its tasks in plan order as their dependencies finish.
A task completes when it passes the completion gate: the reviewer accepted it
under the hard gates (or EMCS overrode a minor-only rejection) and no accepted
test of an earlier task went missing. A task is skipped when it stalls or uses
up its `max_iterations`; the run then continues with the next task. The run
ends:

- **approved**: every task completed
- **partial**: some tasks completed, others were skipped (or every task completed but an accepted test of an approved task broke later); a partial run opens no PR
- **exhausted**: iteration (`max_iterations` per task), cost or wall-time budget reached
- **stalled**: the last pending task stalled — two no-change attempts in a row
  (`no_progress`), or after 3 attempts (5 with a local implementer) the same
  test failure twice (`recurring_failure`) or, without failing tests, ≥70% of
  the review issues repeated (`recurring_issues`)
- **error**: the implementer or reviewer failed twice in a row on a task, the
  first plan failed or had no tasks, the implementer reported it is blocked,
  the plan was rejected (`--interactive`), or the workspace or sandbox could
  not be prepared (e.g. uncommitted changes without `--allow-dirty`)

### State Checkpointing and Resume

The full pipeline state is saved to `runs/{run_id}/state_{iteration}.json` after the plan and at the end of every pass (and on interruption). `delegatron resume <run_id>` reloads the latest checkpoint on the run branch and continues: the plan, task progress and per-task state are restored, and the architect is skipped when a plan exists. The cost adds up across sessions.

---

## 3. Agent Layer

All agents inherit from `BaseAgent` (`src/delegatron/agents/base.py`), which enforces a uniform contract:

```
BaseAgent (abstract)
  ├── _build_user_message(input_data) → str       # Format input for the LLM
  ├── _get_output_schema() → dict                  # JSON Schema for structured output
  ├── _get_output_model() → type[BaseModel]        # Pydantic model for validation
  ├── invoke(input_data) → dict                    # Run agent, return validated artifact
  └── stream_invoke(input_data) → AsyncIterator    # Streaming variant (yields str chunks then dict)
```

### Agent Lifecycle

1. Orchestrator calls `agent.invoke(input_data)` (or `stream_invoke` when streaming is enabled) with structured input
2. Agent builds a user message from the input data (markdown sections, `agents/rendering.py`); when the model's context window is known (the role's `context_window`, or detected from Ollama), the least important input is trimmed first so the prompt fits (`utils/context_budget.py`, reported in `_meta["context_trimmed"]`)
3. Agent calls the LLM provider with system prompt + user message + JSON schema
4. Response is parsed from JSON, validated against the Pydantic model
5. If validation fails, the agent retries up to 2 times with the error appended (`prompts/validation_retry/v1.md`); output cut off at `max_tokens` is retried once with double the budget (at most 32768 tokens, `prompts/truncation_retry/v1.md`) and otherwise raises `TruncatedOutputError`
6. Token usage metadata (`_meta`, summed over every attempt) is attached to the result
7. Orchestrator extracts the artifact and metadata, records costs

### Streaming Agent Calls

`stream_invoke` provides real-time token output without changing the agent contract:

1. Calls the provider's `stream_generate` method, which yields `str` token chunks as they arrive from the LLM
2. Each token chunk is yielded to the caller (orchestrator) for relay to CLI or SSE clients
3. The final item from the provider stream is a `ProviderResponse` containing the full accumulated content and token usage
4. The accumulated response is validated against the Pydantic model just like `invoke`
5. If validation fails (or the output was cut off), `stream_invoke` continues on the non-streaming `invoke` retry path with the streamed response as its first attempt (with error feedback injection); a stream that breaks off with a retryable provider error is retried without streaming
6. The validated artifact dict is yielded as the last item in the stream

On the orchestrator side, `Pipeline._invoke_agent()` is the unified entry point. When `stream=True`, it iterates over `stream_invoke`, relays each `str` chunk as a `token` step-callback event (tagged with the originating role), and returns the final artifact dict.

### Agent Roles

| Agent | File | Default Provider | Output Schema | Responsibilities |
|-------|------|-----------------|---------------|-----------------|
| **Architect** | `agents/architect.py` | Anthropic Claude | `WorkPlan` | Decompose request into tasks, define acceptance criteria, set termination rules |
| **Implementer** | `agents/implementer.py` | OpenAI GPT | `PatchSet` | Generate code patches, add/update tests, signal "blocked" when stuck |
| **Reviewer** | `agents/reviewer.py` | Google Gemini | `ReviewReport` | Assess correctness/security/performance, categorize issues by severity, verdict |
| **Director** | `agents/director.py` | The architect's provider and model | `ArbitrationReport` | Rule once on a conflict that recurs on a task (ACCEPT / REVISE / DEFER); prompt `prompts/director/v1.md` |

### Key Design: Stateless Agents

Agents never accumulate conversation history. Each invocation receives fresh context — the orchestrator injects only what the agent needs for that specific step:

- **Architect**: user request, project info, file tree, review feedback (on re-plan)
- **Implementer**: task spec, plan summary, selected file contents and repo map, failing-test output, review feedback, director guidance, import warnings
- **Reviewer**: original request, diff, test report, acceptance criteria, iteration number
- **Director**: the conflicting issues, the task, the plan's acceptance criteria, the failing-test output and the task's files

This prevents context window bloat and makes each step independently retryable.

---

## 4. Provider Abstraction

The provider layer (`src/delegatron/agents/providers/`) normalizes all LLM APIs to a single interface.

### BaseProvider Interface

```python
class BaseProvider(ABC):
    async def generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> ProviderResponse

    async def stream_generate(
        self,
        system_prompt: str,
        user_message: str,
        json_schema: dict | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> AsyncIterator[str | ProviderResponse]
```

### Provider Streaming Architecture

`stream_generate` is the streaming counterpart to `generate`. It yields `str` token chunks as they arrive from the LLM, followed by a final `ProviderResponse` containing the full accumulated content and token usage metadata.

The base class provides a default implementation that falls back to `generate` and yields the full content as a single chunk. Providers that support native streaming override this with true incremental delivery:

- **AnthropicProvider**: Uses `messages.stream()` context manager, processes `content_block_delta` events (both `text_delta` and `input_json_delta` for tool_use structured output)
- **OpenAIProvider**: Uses `chat.completions.create(stream=True)`, processes streamed `delta.content` chunks

`GoogleProvider` and `MockProvider` use the single-chunk fallback.

The streaming chain flows as: `BaseProvider.stream_generate` → `BaseAgent.stream_invoke` → `Pipeline._invoke_agent` → `step_callback("token", ...)` → CLI stderr / SSE event.

### ProviderResponse

```python
@dataclass
class ProviderResponse:
    content: str              # Raw text/JSON from the model
    model: str                # Model identifier
    input_tokens: int         # Token count for cost tracking
    output_tokens: int
    request_id: str           # Internal tracking ID (dlg-...)
    provider_request_id: str  # Provider's own request ID
    raw_response: Any         # Original provider response object
    finish_reason: str        # "length" when the output hit max_tokens
    cache_read_tokens: int    # Prompt tokens served from the provider's cache
    cache_write_tokens: int   # Prompt tokens written to it
```

Providers make one attempt per call (SDK retries are off) and report failures as `ProviderError` subclasses (`ProviderTimeoutError`, `ProviderTransientError`, `ProviderRequestError`); `utils/retry.py` decides what is retried.

### Provider Registry

`get_provider(role_config: RoleConfig) -> BaseProvider` maps provider names to adapters:

| Provider Key | Adapter | Notes |
|-------------|---------|-------|
| `anthropic` | `AnthropicProvider` | Uses `anthropic` SDK, structured output via tool_use |
| `openai` | `OpenAIProvider` | Uses `openai` SDK, structured output via response_format |
| `google` | `GoogleProvider` | Uses `google-genai` SDK |
| `openai-compatible` | `OpenAIProvider` | Same adapter with custom `base_url` (Ollama, Grok, etc.) |
| `mock` | `MockProvider` | Returns canned responses per role; used for testing |

Any other provider key (for example `litellm`) falls back to `OpenAIProvider`.

### Configuration

Each agent role is configured independently via `RoleConfig`:

```python
@dataclass(frozen=True)
class RoleConfig:
    provider: str        # "anthropic", "openai", "google", etc.
    model: str           # "claude-sonnet-5-5", "gpt-4.1", etc.
    api_key: str         # Explicit key (not recommended)
    api_key_env: str     # Env var name for the key
    base_url: str        # Custom endpoint for openai-compatible
    max_tokens: int      # Default 16384
    temperature: float   # Default 0.0 (deterministic), clamped to [0, 2]
    prompt_version: int  # 0 = by endpoint: v1 for a cloud API, v5 for a local endpoint or mock
    context_window: int  # Tokens; 0 = detected from local Ollama, unlimited elsewhere
    timeout_seconds: int # Per request; default 600 (900 for a local endpoint)
    allow_env_key: bool  # False: never read a key from the environment
```

API keys are resolved via: explicit value → named env var (`api_key_env`) → provider default env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). The default variable is sent only to that provider's own API host: a role with a custom `base_url` gets no key unless it names one with `api_key_env`, and `openai-compatible` has no default variable.

### Presets

Pre-configured role combinations for common use cases:

| Preset | Architect | Implementer | Reviewer | Notes |
|--------|-----------|-------------|----------|-------|
| `default` | Claude Sonnet 4 | GPT-4.1 | Gemini 2.5 Pro | Best quality (cloud) |
| `claude-heavy` | Claude Opus 4.6 | Claude Sonnet 4 | Gemini 2.5 Pro | Complex tasks |
| `budget` | Gemini 2.5 Flash | Gemini 2.5 Flash | Gemini 2.5 Flash | Cheapest cloud |
| `local` | phi4:14b (local) | qwen2.5-coder:14b (local) | phi4:14b (local) | Default local, fits 12 GB VRAM |
| `local-phi4` | phi4:14b (local) | phi4:14b (local) | phi4:14b (local) | Fits 12 GB VRAM, fastest single-model |
| `local-heavy` | phi4:14b (local) | qwen3-coder:30b (local) | phi4:14b (local) | **⚠️ Requires 24 GB+ VRAM.** Spills 30+ GB to RAM on smaller cards |
| `local-oss` | phi4:14b (local) | gpt-oss:20b (local) | phi4:14b (local) | Best local quality (validated 2026-04-18); 13 GB MXFP4, modest CPU offload on 12 GB |
| `local-oss-review` | phi4:14b (local) | qwen2.5-coder:14b (local) | gpt-oss:20b (local) | Strongest local review, ~2× reviewer latency |
| `single-claude` | Claude Sonnet 4 | Claude Sonnet 4 | Claude Sonnet 4 | Baseline (cloud) |
| `single-gpt` | GPT-4.1 | GPT-4.1 | GPT-4.1 | Baseline (cloud) |
| `mock` | Mock | Mock | Mock | Testing |

The local presets also set pipeline defaults: `max_iterations` 5 (10 for `local-heavy`), `max_cost_usd` 10.0, `max_wall_time_minutes` 60 and `emcs_threshold` 0.6.

---

## 5. Orchestrator

The orchestrator (`src/delegatron/orchestrator/`) is the central control plane. It manages the pipeline loop, state transitions, and all side effects.

### Pipeline Class (`orchestrator/graph.py`)

The `Pipeline` dataclass is the main entry point:

```python
@dataclass
class Pipeline:
    config: Config
    workspace_root: Path
    run_id: str
    artifacts_dir: Path | None = None          # Default: <workspace_root>/runs
    base_branch: str = ""                      # Fork point of the run branch ("" = checked-out branch)
    approval_callback: Callable | None = None  # For interactive mode
    step_callback: Callable | None = None      # For SSE streaming
    step_callback_persists: bool = False       # The callback writes events.jsonl itself
    pr_creator: BasePRCreator | None = None    # Injectable; default: the `gh` CLI
    allow_dirty: bool = False                  # Start despite uncommitted changes (--allow-dirty)
```

`Pipeline.execute(request, initial_state=None, stream=False)` runs the full loop and returns the final `AgentState`. When `stream=True`, agent calls use `stream_invoke` and token chunks are relayed through `step_callback`. The `approval_callback` may be sync or async — both are handled via `inspect.isawaitable()`, which allows the server to pass an async callback for plan approval while the CLI uses a synchronous one.

### AgentState (`orchestrator/state.py`)

All pipeline state flows through a single `TypedDict`:

```python
class AgentState(TypedDict):
    messages: list[dict]          # Append-only audit trail
    request: str                  # Original user request
    project_info: dict            # Detected languages, frameworks, etc.
    file_tree: list[str]          # Tracked files in workspace
    workspace_root: str
    plan: dict | None             # Architect output (WorkPlan)
    current_task: dict | None     # Current task from plan
    current_patch: dict | None    # Implementer output (PatchSet)
    test_report: dict | None      # Sandbox output (TestReport)
    review_report: dict | None    # Reviewer output (ReviewReport)
    previous_review: dict | None  # For stall detection
    iteration: int                # Current loop count
    is_approved: bool             # Reviewer accepted
    patch_applied: bool           # The latest patch applied cleanly
    total_cost_usd: float         # Running cost total
    status: str                   # "running"|"approved"|"partial"|"exhausted"|"stalled"|"error"
    error: str                    # Error message if status="error"
    # ...plus task tracking (all_tasks, _completed_tasks, _skipped_tasks, _task_outcomes,
    # _task_iteration), re-plan, arbitration, security, test-history and flaky-test keys
```

The `messages` field uses an append reducer — all events are accumulated for auditability. All other fields use replace semantics (latest value wins). On resume every key except the request, project info, file tree, workspace root, status, error and the per-session `_termination_reason`/`_agent_failures` is restored from the checkpoint (`graph.RESUME_STATE_KEYS`).

### Router (`orchestrator/router.py`)

After each review that neither advances the run to its next task nor stalls the task, `route_after_review(state, max_replans=...)` returns one of:

- `"approved"` — reviewer accepted, finalize
- `"architect"` — a blocker/major issue has `fix_target: "architecture"`, or the orchestrator set a `replan_request` (e.g. a director ruling that the plan must change); re-plan (at most `pipeline.max_replans` re-plans per run, default 2; completed tasks are kept)
- `"implementer"` — implementation issues, fix code
- `"exhausted"` — the run already ended (`exhausted`, `stalled` or `error`)

### Termination (`orchestrator/termination.py`)

`should_terminate(state, max_iterations, max_cost_usd)` checks:
- `is_approved` flag
- iteration count >= max
- cost >= budget (`max_cost_usd` <= 0 means no cost limit)

The pipeline loop applies its own limits before each iteration (`Pipeline._stop_reason`): the run's iteration cap (`max_iterations` per planned task), `max_cost_usd` and the wall time, which also cancels an agent call or sandbox run in progress.

`should_stop_for_stall(state, min_attempts=...)` applies the per-task stall policy and returns its reason (`no_progress`, `recurring_failure`, `recurring_issues`) or `None`; `detect_stall(current_review, previous_review)` (70%+ of issue titles repeat) is its title check.

### EMCS Scoring (`orchestrator/scoring.py`)

Evaluation Matrix Consensus Scoring computes a 0.0–1.0 quality score:

- Starts at 1.0, deducted by severity weights: blocker=10, major=5, minor=1, nit=0.2
- Normalized against max_possible_deductions (20.0)
- +0.3 bonus for passing tests
- Blocker and major issues always block regardless of score, as do a patch that did not apply and failing tests
- Score >= threshold (`emcs_threshold`, default 0.8; 0.6 in the local presets) can override a reviewer rejection only when every issue it names is minor or nit and real tests passed

### Arbitration (`orchestrator/arbitration.py`)

When implementer and reviewer ping-pong on a task — the same test failure twice, or the same review issues — the conflict goes to the director (`agents/director.py`, `prompts/director/v1.md`), which runs on the architect's model. Each conflict is ruled on once per task. The director sees the task, the failing-test output and the task's files; the orchestrator's own gate issues (removed tests, blocking security findings) are evidence and never arbitrated. A test failure recurs when the same tests fail with the same error shape (`utils/test_output.failure_signature`): numbers, quoted literals, durations and memory addresses in the error line are masked, so `AssertionError: 12 != 10` followed by `AssertionError: 14 != 12` on the same test is one recurring failure. Rulings, per conflict:

- **ACCEPT**: Dismiss the reviewer's concern (approves only with real passing tests)
- **REVISE**: One concrete change (`fix_target`/`fix_file`) for the next implementer attempt, which the stall policy grants
- **DEFER**: Downgrade to nit severity (won't block approval)

A director failure (`arbitration_failed`) changes nothing. Each ruling is saved as `arbitration_{iteration}.json`; `pipeline.enable_arbitration = false` turns arbitration off.

### Fan-Out (`orchestrator/fanout.py`)

Groups the pending tasks of the WorkPlan into execution waves by their dependencies. With `parallel_tasks`, the first patches of the ready tasks are generated concurrently in git worktrees of the run branch; each is then applied, tested, reviewed and gated as its own task in plan order.

### Async Runner (`orchestrator/async_runner.py`)

Deprecated standalone runner (it works only the first planned task and applies only `diff_unified` patches). The server and the queue run `graph.Pipeline`.

---

## 6. Sandbox Execution

The sandbox layer (`src/delegatron/sandbox/`) provides isolated code execution. Agent-generated code never runs on the host.

### Interface

```python
class BaseSandbox(ABC):
    async def apply_and_test(self, patch: dict) -> dict  # Returns TestReport
    def cleanup(self) -> None                            # Called when the run ends
```

`factories/sandbox_factory.create_sandbox()` picks the sandbox from `pipeline.sandbox` (`docker`, `e2b`, `local`; `none` is an alias of `local`). Toolchain rules (test/install commands and Docker images per language) live in `sandbox/toolchains.py`; `sandbox/prepare.py` re-derives the install commands on every run and selects a patch's `targeted_tests`; `sandbox/counts.py` parses test counts for every runner.

### Docker Sandbox (`sandbox/docker_runner.py`)

Primary sandbox for self-hosted deployments. Uses a **2-phase execution model**:

1. Copies workspace to `<workspace>/_sandbox/workspace` (excluding Delegatron's own directories and `logs` at the root, and `.git`, `__pycache__`, `.venv`, `venv`, `.pytest_cache`, `.mypy_cache`, `.tox` at any depth)
2. Writes new/modified files from the patch (via `files` dict or unified diff) and removes its `delete_files`
3. **Parse gate** (`sandbox/parse_check.py`): syntax-checks every written file before anything is installed; a parse error fails the iteration
4. **Phase 1 (install)**: Runs dependency installation in a Docker container WITH network access; the container is committed as a cached image keyed by the image, the install commands and the dependency manifests, so later iterations skip the install while those are unchanged
5. **Phase 2 (test)**: Runs the patch's targeted tests, then the test commands, in a Docker container WITHOUT network (`--network none`); failing pytest ids are rerun to spot flaky tests, and generated scripts (`entrypoints`) are smoke-run
6. Captures stdout/stderr, exit code and per-test results
7. Cleans up the sandbox directory; `cleanup()` removes the cached images, volumes and leftover containers when the run ends

The 2-phase approach allows `pip install` to fetch packages while ensuring test execution is fully network-isolated. When the install image cannot be committed, Python packages go into a venv in the bind-mounted workspace (other toolchains' downloads onto named volumes) that the test container reuses.

**Security hardening:**
- `--network none` — no network access from container
- `--memory 512m` — memory limit
- `--cpus 1` — CPU limit
- 600-second timeout per container (the container is force-removed)
- Path traversal prevention: `_validate_sandbox_path()` ensures all file writes stay within the sandbox
- Shell command sanitization: `sanitize_shell_command()` rejects patch-supplied test commands containing shell metacharacters (`;`, backticks, `$`, `|`, `>`, `<`, `\`, `&`, `../`) or control characters such as a newline; the sandbox run then reports a failure instead of executing them
- If the `docker` CLI is missing, the sandbox is not created (`SandboxUnavailableError`); if the daemon is unavailable, the run reports a failure (no fallback to host execution)

### E2B Sandbox (`sandbox/e2b_runner.py`)

Cloud sandbox using E2B Firecracker microVMs (~150ms cold starts):

1. Creates an E2B sandbox session via `AsyncSandbox.create()`
2. Uploads workspace files into `/home/user/workspace` (skips `.git`, `__pycache__`, `.venv`, `venv`, `node_modules`, `.mypy_cache`, Delegatron's own directories and files that are not UTF-8 text)
3. Applies patch (files dict → direct write, deletions, or unified diff → `patch -p1`)
4. Runs install + test commands
5. Captures results
6. Destroys the sandbox

**Additional security:**
- Symlink validation: skips symlinks that escape the workspace
- Path validation: rejects absolute paths and `..` components in patches
- Shell-quoted paths to prevent injection from special characters

### Local Sandbox (`sandbox/local_runner.py`)

Development/testing sandbox with no isolation. Runs commands directly on the host: the patch is staged into the workspace for the run and put back afterwards, with a per-workspace venv under the system temp dir. Generated scripts are smoke-run only with `run_entrypoints = true`. Only suitable for trusted code during development — never for production or untrusted inputs.

### Diff Parsing

Both Docker and E2B sandboxes include `_parse_diff_to_files()` which extracts new file contents from unified diffs (handles `--- /dev/null` for new files). For modifications to existing files, falls back to the `patch` command.

---

## 7. Schema Contract

All inter-agent communication uses Pydantic v2 models in `src/delegatron/schemas/`. No free-form chat between agents — everything is schema-validated JSON.

### WorkPlan (`schemas/work_plan.py`)

Architect output — task decomposition:

```python
class WorkPlan(BaseModel):
    summary: str
    assumptions: list[str]
    open_questions: list[str]
    tasks: list[Task]            # task_id, title, description, outputs, risk_level, depends_on
    acceptance: AcceptanceCriteria  # criteria + tests
    termination: TerminationPolicy # done_when + stop_limits (max_iterations, max_cost, max_time)
```

Before execution, `orchestrator/plan_normalize.py` makes the plan consistent (duplicate or empty task ids, `depends_on` entries naming no task, unsafe or messy output paths, test outputs outside the test layout).

### PatchSet (`schemas/patch_set.py`)

Implementer output — code changes:

```python
class PatchSet(BaseModel):
    status: Literal["ok", "blocked"]
    files: dict[str, str] = {}    # Full file contents keyed by relative path (primary)
    edits: list[FileEdit] = []    # Search/replace edits of existing files (path, search, replace, count)
    delete_files: list[str] = []  # Paths to remove
    diff_unified: str = ""        # Unified diff (legacy fallback)
    rationale: str = ""           # Why these changes
    tests_added_or_updated: list[str]
    tests_to_run: list[str]
    questions: list[str]          # When status="blocked"
```

### TestReport (`schemas/test_report.py`)

Sandbox output — test results:

```python
class SandboxReport(BaseModel):
    passed: bool
    total: int
    failures: int
    results: list[SandboxTestResult]  # One per pytest node id (status, first error line)
    stdout: str
    stderr: str
    duration_ms: float
    failed_tests: list[str]           # Node ids that failed or errored
    failure_excerpt: str              # Failure tracebacks + short summary
    flaky_tests: list[str]            # Failed, then passed on rerun
    entrypoint_runs: list[EntrypointRun]
    targeted: TargetedRun | None      # The task's targeted tests, run first
    lint_warnings: list[str]          # Non-blocking parse-gate advisories
    # ...plus output_truncated, rerun_attempts, rerun_skipped, skipped_reason,
    # synthetic_tests, raw_passed, passed_with_flaky
```

### ReviewReport (`schemas/review_report.py`)

Reviewer output — code review:

```python
class ReviewReport(BaseModel):
    summary: str
    acceptance_met: bool
    issues: list[ReviewIssue]  # severity (blocker|major|minor|nit), title, details, recommended_fix,
                               # evidence, fix_target (code|test|architecture), fix_file
```

### ArbitrationReport (`schemas/arbitration.py`)

Director output — one ruling per conflicting issue:

```python
class ArbitrationReport(BaseModel):
    resolutions: list[Resolution]  # title, decision (ACCEPT|REVISE|DEFER), reasoning,
                                   # alternative, fix_target, fix_file
```

### PipelineEvent (`schemas/events.py`)

Event envelope for observability:

```python
class PipelineEvent(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime
    run_id: str
    task_id: str
    attempt: int
    producer: str            # "orchestrator"
    payload: EventPayload    # agent, artifact_uri, tokens, latency_ms
```

### Standalone JSON Schemas

Machine-readable schemas are also exported in `schemas/*.schema.json` for external tooling and API validation.

---

## 8. Server Layer

The FastAPI server (`src/delegatron/server/app.py`) exposes the pipeline as an HTTP API with SSE streaming. The server has grown significantly since the initial implementation — see `docs/api-reference.md` for full request/response schemas and examples.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check (`{"status": "ok"}`) |
| `GET` | `/api/health/full` | Full system check (Ollama, Docker, disk, providers) |
| `GET` | `/api/defaults` | Server defaults for dashboard form pre-population |
| `GET` | `/api/provider-keys` | Check which LLM provider API keys are configured |
| `POST` | `/api/ollama/restart` | Restart Ollama to clear stuck models (admin role when auth is enabled) |
| `GET` | `/api/stream-token` | Signed URL for SSE (prevents API key in query params) |
| `GET` | `/api/system/stats` | Live GPU, Docker, and pipeline stats |
| `GET` | `/api/queue` | Current run queue status |
| `POST` | `/api/runs` | Start a new pipeline run (background task + SSE) |
| `POST` | `/api/runs/plan` | Plan-only: run architect synchronously, return plan |
| `GET` | `/api/runs` | List all runs (filtered by team) |
| `GET` | `/api/runs/{id}` | Get run summary (enriched with files, test results, review) |
| `DELETE` | `/api/runs/{id}` | Delete a run, its artifacts and its run workspace (cancels it if running) |
| `POST` | `/api/runs/{id}/cancel` | Cancel a running pipeline |
| `POST` | `/api/runs/{id}/retry` | Smart retry: a refine when review issues are unresolved, else a fresh rerun (`fresh_start` forces the rerun) |
| `POST` | `/api/runs/{id}/rerun` | Re-run a completed job with the same config (prefer `/retry`) |
| `POST` | `/api/runs/{id}/resume` | Continue an error/cancelled/stalled/exhausted run from its last checkpoint (same run ID and run workspace) |
| `POST` | `/api/runs/{id}/refine` | Re-run focusing on unresolved review issues (prefer `/retry`) |
| `GET` | `/api/runs/{id}/plan` | Get pending plan for interactive approval |
| `POST` | `/api/runs/{id}/approve` | Approve a pending plan |
| `POST` | `/api/runs/{id}/reject` | Reject a pending plan |
| `GET` | `/api/runs/{id}/events` | SSE stream of live events |
| `GET` | `/api/runs/{id}/files` | Contents of every file the run created or modified, across all iterations |
| `GET` | `/api/runs/{id}/compliance` | Export zero-cloud compliance report |
| `GET` | `/api/runs/{id}/download` | Download every file the run created or modified (plus a README) as zip |
| `GET` | `/api/runs/{id}/iterations` | List iteration checkpoints with metadata |
| `GET` | `/api/runs/{id}/iterations/{n}/diff` | Per-iteration diff and artifacts |
| `POST` | `/api/runs/{id}/restore` | Reset a finished run's workspace to the commit of an earlier iteration |
| `POST` | `/api/estimate` | Estimate cost range for a run (by preset/models) |
| `POST` | `/api/review` | Read-only code review or log analysis |
| `GET` | `/api/reviews` | List past code reviews |
| `POST` | `/api/projects` | Start multi-milestone project |
| `GET` | `/api/projects` | List projects |
| `GET` | `/api/projects/{id}` | Get project status and milestone results |
| `GET` | `/api/templates` | List all saved templates |
| `POST` | `/api/templates` | Create a new template |
| `DELETE` | `/api/templates/{name}` | Delete a template |
| `POST` | `/api/templates/{name}/run` | Run a pipeline from a saved template |
| `POST` | `/api/benchmark` | Start benchmark: run task with multiple presets |
| `GET` | `/api/benchmark/{id}` | Get benchmark status and results |
| `POST` | `/api/swebench` | Start SWE-bench evaluation |
| `GET` | `/api/swebench/{id}` | Get evaluation status and results |
| `GET` | `/api/swebench` | List SWE-bench evaluations |
| `GET` | `/api/analytics` | Team-scoped usage analytics |
| `GET` | `/api/analytics/leaderboard` | Preset performance leaderboard |
| `PUT` | `/api/settings/log-level` | Change log level at runtime |
| `PUT` | `/api/settings/log-timezone` | Switch UTC/local timestamps |
| `PUT` | `/api/settings/parallel-tasks` | Enable/disable parallel task execution |
| `PUT` | `/api/settings/project-mode` | Enable/disable project mode |
| `POST` | `/api/webhooks/github` | GitHub webhook handler (issue_comment triggers) |

### Run Lifecycle (Server)

1. `POST /api/runs` authenticates the caller (when auth is on) and validates the request
2. Pipeline launches as `asyncio.create_task()` (stored in `_background_tasks` set to prevent GC). Runs are serialized: while another run holds the run slot (`_run_semaphore`, one run at a time) the new run emits `queued` and waits
3. Each run executes in its own run workspace, `<workspace>/.delegatron-runs/<run_id>` (`server/run_workspace.py`): a clone of the base repository (same history and `origin`, so the run branch can be pushed as a PR) or a fresh repository; its path is recorded in `meta.json`
4. Events are broadcast to subscriber queues (`_active_streams[run_id]`)
5. SSE clients connect via `GET /api/runs/{id}/events` and receive the events saved so far, then live events from a per-subscriber bounded queue (maxsize=256)
6. On completion, summary is persisted and stream is closed after 5s delay

#### API Request Flow (Full Run — Async with SSE)

```
  Client                          Server                       Pipeline
    │                               │                             │
    │  POST /api/runs {request}     │                             │
    │──────────────────────────────►│                             │
    │                               │  Auth (Bearer token)        │
    │                               │  Validate request fields    │
    │                               │                             │
    │  {run_id, status: "started"}  │                             │
    │◄──────────────────────────────│                             │
    │                               │  asyncio.create_task()      │
    │                               │────────────────────────────►│
    │                               │                             │
    │  GET /api/runs/{id}/events    │                             │
    │──────────────────────────────►│                             │
    │                               │  Create subscriber queue    │
    │                               │                             │
    │  SSE: {event: "started"}      │◄────── broadcast ──────────│
    │◄──────────────────────────────│                             │
    │  SSE: {event: "architect..."}│◄────── broadcast ──────────│
    │◄──────────────────────────────│                             │
    │  SSE: {event: "token", ...}  │◄────── broadcast ──────────│
    │◄──────────────────────────────│  (streaming tokens)         │
    │  ...                          │                             │
    │  SSE: {event: "complete"}    │◄────── broadcast ──────────│
    │◄──────────────────────────────│                             │
    │  SSE: event: done             │  Clean up after 5s delay    │
    │◄──────────────────────────────│                             │
```

#### API Request Flow (Plan-Only — Synchronous)

```
  Client                          Server
    │                               │
    │  POST /api/runs/plan          │
    │  {request, preset, ...}       │
    │──────────────────────────────►│
    │                               │  Auth (Bearer token)
    │                               │  Validate request
    │                               │  Load config
    │                               │  Create Architect agent
    │                               │  await _run_architect()
    │                               │     (synchronous — no background task)
    │                               │
    │  {run_id, status: "planned",  │
    │   plan_summary, tasks,        │
    │   assumptions, cost_usd, plan}│
    │◄──────────────────────────────│
    │                               │
```

#### API Request Flow (Benchmark — Async)

```
  Client                          Server
    │                               │
    │  POST /api/benchmark          │
    │  {request, presets: [A,B,C]}  │
    │──────────────────────────────►│
    │                               │  Auth + Validate
    │  {benchmark_id, "running"}    │
    │◄──────────────────────────────│
    │                               │  Background: for each preset
    │                               │    load config ──► full pipeline run
    │                               │    record result
    │                               │  Find cheapest passing preset
    │  GET /api/benchmark/{id}      │
    │──────────────────────────────►│
    │  {status, results[], ...}     │
    │◄──────────────────────────────│
    │                               │
```

### SSE Streaming

Live events use server-sent events (SSE). Multiple clients can subscribe to the same run — each gets an independent queue (fan-out pattern). If a run is already complete, historical events are replayed from the JSONL event log on disk.

### Request Validation

- `request`: 1–100,000 characters
- `preset`: alphanumeric + hyphens/underscores, must exist in PRESETS (`"auto"` picks one with `autoselect.select_preset`)
- `config_path`: relative only, no `..`; must name an existing file inside the server's workspace and outside Delegatron's own directories (run artifacts, run workspaces), since it is trusted as operator config
- `workspace`: an existing directory (under `DELEGATRON_WORKSPACE_ROOT` when set) or a git URL on a host in `DELEGATRON_GIT_ALLOWED_HOSTS` (default github.com, gitlab.com, bitbucket.org); `branch` picks the branch to clone
- `sandbox`: `docker` or `e2b`
- `max_iterations`: 1–100
- `max_cost_usd`: 0–1000 (0 = no cost limit)
- `max_wall_time_minutes`: 1–1440

### GitHub Webhook Integration

The `/api/webhooks/github` endpoint handles `issue_comment` events. Comments starting with `/delegatron run <request>` trigger a pipeline run. Requires `DELEGATRON_WEBHOOK_SECRET` for HMAC-SHA256 signature verification. Webhook-triggered runs are disabled when auth is enabled (no repo→team binding yet).

---

## 9. Authentication and Multi-Tenancy

### Authentication (`server/auth.py`)

- Controlled by `DELEGATRON_AUTH_ENABLED` env var (default: false/dev mode)
- API keys use `dlg_` prefix + 48 hex characters
- Keys stored as SHA-256 hashes (raw key returned only at creation)
- Bearer token auth: `Authorization: Bearer dlg_...`
- Key expiration support
- Loud startup warning when auth is disabled

### Team Management (`server/teams.py`)

- SQLite-backed team storage
- Teams have an owner (admin) and members with roles: `admin`, `member`, `viewer`
- All runs are scoped to a team — `meta.json` records `team_id` and `user_id`
- Team A cannot see Team B's runs (enforced at list/get endpoints)

### Database (`server/db.py`)

SQLite with tables: `api_keys`, `teams`, `team_members`. Schema is auto-initialized at startup via `init_db()`.

---

## 10. Security Subsystem

### Secrets Scanner (`security/secrets.py`)

Scans the lines a patch adds for leaked credentials. Implementers return whole files, so `security/patch_lines.py` diffs each file against what it held before the run first changed it (`SecurityBaseline`): unchanged lines are not attributed to the patch, and a finding from an earlier, already applied patch is reported until it is removed.

- **Pattern-based detection**: AWS access keys, GitHub tokens/PATs, Stripe keys, private keys (PEM), connection strings, generic API keys/tokens
- **Entropy-based detection**: High-entropy strings (Shannon entropy >= 4.5 for base64, >= 3.5 for hex) in assignment contexts (lines containing `key=`, `secret=`, `token=`, etc.)
- **Allowlist support**: Regex patterns to suppress known false positives (test fixtures), from `[security] secrets_allowlist`
- **Redaction**: Findings include redacted line content (the text before the secret, at most 60 chars, + the secret's first 4 chars + `****REDACTED****`)

Findings are logged as pipeline events with severity (critical/high/medium) and shown to the agents. `pipeline.security_mode` decides what they do: `allow` (findings are not shown to the agents), `warn` (default) or `block`, where constraint violations and secrets (critical → blocker, otherwise major) keep the patch out of the workspace and block approval. New dependencies are minor findings and never block. The run summary's `security` section counts the unresolved, detected and blocked findings.

### Dependency Auditor (`security/deps.py`)

Detects new third-party imports introduced by patches:

1. Extracts Python import statements from the lines a patch adds
2. Classifies each as: stdlib / declared dependency / local module / new
3. Reads declared deps from `pyproject.toml`, `requirements.txt`, `setup.cfg` with the patch overlaid on the workspace (a manifest or module added by the same patch counts)
4. Handles package-name-to-import-name mappings (e.g., `pillow` → `PIL`, `scikit-learn` → `sklearn`)
5. New dependencies flagged for human review

### Sandbox Command Sanitization (`sandbox/base.py`)

`sanitize_shell_command()` blocks shell metacharacters: `;`, backticks, `$`, `|`, `>`, `<`, `\`, `&`, `../`, and control characters other than tab (a newline would start a second command). It applies to agent-supplied commands and raises `UnsafeCommandError`, so a blocked command never runs in any form: a blocked `test_commands` entry of a patch fails the sandbox run, an unsafe `targeted_tests` entry is skipped; infrastructure commands (factory, project detection, toolchain rules) are trusted and not passed through it.

### Import Validator (`security/import_validator.py`)

Checks the imports of a patch (JavaScript/TypeScript, Python, Go, Rust, best-effort Java) against the project's declared packages, with the patch overlaid on the workspace. Advisory only: the findings (`import_issues`) go to the reviewer, and to the next implementer as `import_warnings` after a failed test run; they never gate acceptance on their own.

### Zero-Cloud Verification (`security/zero_cloud.py`)

After each run, a `NetworkGuard` report records every network interaction the orchestrator made (LLM calls, sandbox execution, artifact storage, other egress such as an E2B upload, a `git push` or the failure webhook) and whether each stayed on the local network; it is saved as `zero_cloud_report.json` and in the summary's `zero_cloud` section. `pipeline.local_hosts` / `DELEGATRON_LOCAL_HOSTS` name extra hosts that count as local.

### Path Containment

Multiple layers of path traversal prevention:
- `utils/paths.normalize_rel_path()`: the shared check every writer, copier and lister uses on a model-supplied path — rejects absolute paths, drive letters, UNC paths, `..`, `.git` in any spelling (and `git~1`), `:` and reserved Windows device names
- `Workspace.apply_files()` / `render_edits()`: skip and report unsafe patch paths instead of writing them
- `Workspace.read_file()`: validates resolved path stays within repo boundary
- `DockerSandbox._validate_sandbox_path()`: ensures file writes stay within sandbox dir
- `E2BSandbox._validate_e2b_path()`: rejects absolute paths and `..` components
- `ArtifactStore.__init__()`: validates run_id format and resolved path stays within base dir
- `RunRequest.validate_config_path()`: blocks `..` and absolute paths in config_path; the server also requires it to resolve inside its workspace and outside Delegatron's own directories
- `context.select_context()`: prevents path traversal when reading files for implementer

---

## 11. Supporting Subsystems

### Cost Tracking (`utils/cost.py`)

`CostTracker` records every LLM API call with:
- Model name, role, iteration
- Input/output token counts
- Estimated cost (per-model pricing table)
- Internal request ID + provider request ID

Pricing table (per million tokens):

| Model | Input | Output |
|-------|-------|--------|
| Claude Sonnet 4 | $3.00 | $15.00 |
| Claude Opus 4.6 | $15.00 | $75.00 |
| GPT-4.1 | $2.00 | $8.00 |
| Gemini 2.5 Pro | $1.25 | $10.00 |
| Gemini 2.5 Flash | $0.30 | $2.50 |

Prompt-cache reads and writes are billed at their own rates where the table has them. An unpriced cloud model is assumed to cost $5.00 / $15.00 (logged once, since the guess counts toward `max_cost_usd`); local models cost no API fee but get an electricity + GPU amortization estimate (`DELEGATRON_ELECTRICITY_RATE`, `DELEGATRON_GPU_TDP_WATTS`, `DELEGATRON_GPU_PRICE_USD`, `DELEGATRON_GPU_LIFETIME_HOURS`).

Call records are appended to `runs/{run_id}/provider_calls.jsonl` for full auditability.

### Retry (`utils/retry.py`)

`retry_with_backoff(fn, max_retries=3, base_delay=1.0, max_delay=60.0)` — exponential backoff with jitter for all LLM API calls; the single retry layer (provider SDK retries are off). Only transient errors retry (`is_retryable`): timeouts, connection errors, HTTP 408/409/429 and 5xx; other 4xx errors, `ValueError` and `TypeError` fail at once. A provider's `retry_after` sets the minimum wait. Each request is bounded by the role's `timeout_seconds` (600 s, as in the Anthropic and OpenAI SDKs; 900 s for local endpoints), and `BaseAgent` adds a deadline of 1.5× that per call. A streamed call is bounded per gap between chunks, not as a whole.

### Prompt Management (`prompts.py`)

- Prompts stored in `prompts/{role}/v{n}.md` (external files, not hardcoded)
- `load_prompt(role, version)` loads and caches (LRU) prompt text
- `get_latest_version(role)` finds the highest version number
- `list_prompt_versions(role)` lists all available versions
- Supports per-role prompt versioning in config (`prompt_version` field; 0 resolves by endpoint in `factories/agent_factory.resolve_prompt_version`)
- Besides the `architect/`, `implementer/` and `reviewer/` sets: `director/v1.md`, `meta_architect/v1.md` (project mode) and the retry notes `validation_retry/v1.md` and `truncation_retry/v1.md`

### Project Detection (`detection.py`)

Scans workspace for language/framework markers:

- **Languages**: Python, JavaScript/TypeScript, Go, Rust, Java, Kotlin, Ruby, PHP, C#, F#, C/C++, Swift, Dart, Elixir, Scala, Haskell, PowerShell, and more (20+ total)
- **Frameworks**: Django, FastAPI, Flask, Next.js, Nuxt, Svelte, Angular, etc.
- **Tooling**: test commands, lint commands, package manager
- Detection via marker files (e.g., `pyproject.toml` → Python/pip) and dependency scanning

### Git Workspace (`workspace.py`)

Manages an isolated git branch per pipeline run:

1. `initialize()` — creates the `delegatron/run-{run_id}` branch (`delegatron/run-{run_id}-{n}` when a fresh run finds that name taken; resume reuses it) from the requested base branch, else from the checked-out branch. Uncommitted changes outside Delegatron's own directories raise `DirtyWorkspaceError` unless `allow_dirty` (`--allow-dirty`) is set
2. `apply_files(files, delete_files)` — writes whole files and deletes paths (the implementer's usual output); `render_edits(edits)` applies search/replace edits in memory first
3. `apply_patch(diff)` — applies unified diff via `git apply` (check + apply)
4. `commit(message)` — commits only the paths the run wrote or deleted since the last commit, so the user's own changes never land in a run commit (repository git hooks are skipped unless `pipeline.run_git_hooks`)
5. `rollback(to_sha)` — resets to a specific commit
6. `get_diff_from_base()` — full diff since branching
7. Falls back to the remote's default branch only when HEAD is detached

Path traversal protection on `read_file()`.

### Context Gathering (`context.py`)

Selects relevant file contents to feed the implementer (instead of just a file tree), in priority tiers (`select_context`):

1. Files from task outputs
2. Files of the failing tests, each followed by the local modules it imports
3. Files cited by review feedback (`file`, `fix_file`, `evidence`, `recommended_fix`), each followed by the local modules it imports
4. Files written by earlier tasks
5. Test ↔ source counterparts of tiers 1–4 (`test_x.py`, `tests/test_x.py`, `x_test.py`; root-level and `src/` layouts) and package `__init__.py` files
6. Files the task description names
7. Counterparts of tier 6, sibling source files in the same directories, local modules imported by the files above or named in the task description, then the repository root's other source files (only when the root holds at most 20)

Capped at 50KB per file and 50KB total; with a known implementer context window the total shrinks to 60% of the input tokens left beside the system prompt, schema and response reserve (`Pipeline._context_budget`, at least 2KB). A structural repo map (`repomap.py`) is passed alongside.

#### Context Gathering Flow

```
  Current task + failing tests + review feedback + earlier tasks' files
       │
       ▼
  ┌─────────────────────────────┐
  │  Tiers 1-7 (see above)      │
  └──────────────┬──────────────┘
                 │
                 ├──► Read each file (skip non-existent and binary, block path traversal)
                 ├──► Tiers 1-4 and 6: a file larger than the budget left
                 │    is shown as a head + tail excerpt
                 ├──► Tiers 5 and 7: a file that does not fit is left out
                 │
                 ▼
  ┌─────────────────────────────┐
  │  {path: content} dict       │──► Passed to Implementer as existing_files
  └─────────────────────────────┘      (+ partial_files, omitted_files, binary_files)
```

Before the call, the agent's context-window guard (`utils/context_budget.py`) can trim further: the repo map first, then related files, and the files the attempt must edit last.

### Artifact Store (`artifacts.py`)

Persists all pipeline artifacts to `runs/{run_id}/` (`Pipeline.artifacts_dir`, default `<workspace>/runs`; the server uses `DELEGATRON_ARTIFACTS_DIR`, else `runs/` in its install directory):

```
runs/{run_id}/
  work_plan.json              # Architect output
  work_plan_r1.json           # Re-plans (one per revision)
  baseline_test_report.json   # Pre-run baseline test report
  patch_set_001.json          # Implementer outputs (per iteration)
  test_report_001.json        # Sandbox outputs (per iteration)
  review_report_001.json      # Reviewer outputs (per iteration)
  arbitration_001.json        # Director rulings (iterations with a conflict)
  state_001.json              # Full state checkpoints (for resume)
  summary.json                # Final run summary
  zero_cloud_report.json      # Network egress report of the run
  events.jsonl                # Append-only event log
  provider_calls.jsonl        # LLM API call log
  meta.json                   # Team/user metadata (server runs)
```

`summary.json` holds the run's `run_id`, `request`, `plan_summary`, `status`, `is_approved`, `error`, `workspace_root`, `iterations`, cost (`total_cost_usd`, `prior_cost_usd`, `session_cost_usd`) and elapsed time (also split by session), `termination_reason`, `branch`, `files_changed` / `files_deleted`, `task_outcomes` and `skipped_tasks`, `baseline_failures`, `fixed_baseline_failures`, `regressed_tests`, `flaky_tests`, `security`, `roles`, `cost_breakdown`, `observer_metrics`, `zero_cloud`, and `pull_request` when a PR was attempted; `refine` adds `refinement_rounds`.

Run ID validation: alphanumeric + hyphens/underscores, 1-64 chars. Path containment enforced.

### Constraints (`constraints.py`)

User-defined rules for generated code:

- `no_modify`: gitignore-style patterns for files that must not be changed
- `required_patterns`: text that must appear in at least one changed source file of each patch
- `forbidden_patterns`: text that must NOT appear in the lines a patch adds
- `guidelines`: prose rules shown to the agents, never pattern-checked

Pattern entries are literal text unless prefixed with `re:` (a regular expression; an invalid one fails at load time). Loaded from the `[constraints]` and `[pipeline.constraints]` sections of `delegatron.toml`, merged with run-template and API constraints, and shown to all three agents.

### Analytics (`analytics.py`)

Usage tracking and aggregation: `AnalyticsStore` records per-run metrics (preset, status, iterations, cost, duration, model usage) to a JSONL log file for historical analysis. Nothing in the pipeline or server writes to it yet; `GET /api/analytics` and `/api/analytics/leaderboard` derive their numbers from the runs' `summary.json` files.

### Plugins (`plugins.py`)

Extension point for custom agents, providers, sandboxes and lifecycle hooks: a plugin registry with entry-point and local discovery, and a hook manager (pre/post agent steps). It is a library API only — the pipeline, CLI and server neither load plugins nor emit hooks yet (see `docs/plugins.md`).

### Auto-Selection Module (`autoselect.py`)

Heuristic-based preset selection for users who don't know which configuration to choose. The `select_preset(request, project_info)` function scores the task on three dimensions:

1. **Request text analysis**: Scans for fast keywords (simple, quick, typo, rename) and heavy keywords (security, refactor, architecture, migrate, distributed). Multi-file references add weight.
2. **Language difficulty**: Projects using Python/JS/TS score lower (well-supported by all models); projects using Rust/Go/Haskell/C++ score higher (benefit from larger models).
3. **Project size**: Projects with 50+ tracked files push toward heavier presets.

The aggregate score maps to: `budget` (score <= -1), `default` (score 0-1), or `claude-heavy` (score >= 2). A human-readable reason string is returned alongside the preset name.

Invoked via `--auto` on the CLI; the server auto-selects when a run request sets `"preset": "auto"` and otherwise uses the given preset or the default.

### Template System (`templates.py`)

Reusable run configurations with parameterized request patterns. A `RunTemplate` contains:

- **`request_pattern`**: A template string with `{placeholders}` (e.g., `"Add {feature} to {module}"`)
- **`preset`**: Optional preset override
- **`overrides`**: Pipeline config overrides (max_iterations, per-role model overrides, etc.)
- **`constraints`**: Project-level constraints applied to generated code
- **`variables`**: Default values for placeholders

`TemplateStore` provides CRUD persistence as JSON files in a configurable directory (defaults to `~/.delegatron/templates/` for CLI, `$DELEGATRON_DATA_DIR/templates/` — `data/templates/` by default — for server), on top of the 30 built-in templates shipped in `templates/`. Templates are managed via `delegatron template` CLI subcommands and the `/api/templates` REST endpoints.

When a template is run, its `request_pattern` is rendered with user-supplied variable overrides merged on top of defaults, then submitted as a normal pipeline run with the template's preset and overrides applied.

### Benchmark System (`benchmark.py` / `benchmark_suite.py`)

Two complementary benchmarking tools for empirically comparing pipeline configurations:

**`benchmark.py` — Preset Comparison**: Runs the same user task with multiple presets sequentially. For each preset, records: status, iteration count, total cost, wall-clock time, EMCS quality score, and approval status. The `BenchmarkReport` identifies the best preset by quality, cheapest, and fastest.

**`benchmark_suite.py` — Regression Suite**: Runs a collection of `BenchmarkCase` scenarios (built-in cases include hello_world, crud_endpoint, and bug_fix) to validate pipeline quality over time. Supports a `mock` preset for offline/CI testing (returns synthetic passing results). The `SuiteReport` aggregates pass rate, total time, and total cost across all cases.

Both are accessible via `delegatron benchmark` (CLI) and `POST /api/benchmark` (server). Server-side benchmarks run as background tasks with status polling via `GET /api/benchmark/{id}`.

### Local Model Optimizations

Several subsystems include special handling for local models (Ollama/OpenAI-compatible endpoints):

- **v5 prompts** (`prompts/{role}/v5.md`): Prompt version for the local presets. A role whose `prompt_version` is 0 (unset) gets v5 on a local endpoint and v1 on a cloud one.
- **v2–v4 prompts** (legacy): Earlier iterations for local models.
- **JSON repair** (`BaseAgent._parse_response`): Multiple recovery strategies for malformed LLM output common with local models — strips `<think>` reasoning blocks (Qwen3), removes markdown code fences, performs iterative scan for the first valid JSON object in surrounding prose, falls back to greedy regex extraction.
- **Connection error handling**: `OllamaConnectionError` (in `openai_provider.py`, a `ProviderError`) names Ollama connectivity failures; a refused connection or a plain 500 is retryable, a missing model or out-of-memory error is not. A timeout is reported as a timeout, with the model and the timeout.
- **Context budget**: The context gathering module's 50KB total was chosen to avoid overwhelming local models; with a known (configured or Ollama-detected) context window the file budget shrinks to fit it, and the agents' context-window guard trims the prompt before each call (a `context_trimmed` event).

### GPU Cleanup (`utils/gpu_cleanup.py`)

Automatic GPU memory management for Ollama models after pipeline runs. Ollama keeps models resident in VRAM for a configurable keep-alive period, which can exhaust GPU memory when using multi-model presets (e.g., `local` with architect + implementer models).

- `cleanup_after_run(config)` — Unloads all Ollama models used in the pipeline after completion. Called from `Pipeline.execute()` finalization. Non-blocking (errors are logged, never propagated).
- `unload_except(config, keep_model)` — Unloads all models EXCEPT the one about to be used. Called before each invocation of a role with a `base_url` to ensure only the active model occupies VRAM. Critical for systems with limited GPU memory (e.g., 12GB) running multi-model presets.

### Multi-Language Sandbox Detection

The Docker sandbox (`sandbox/docker_runner.py`) picks the toolchain from the rules in `sandbox/toolchains.py` (20+ languages) and configures the appropriate Docker image, install commands, and test commands:

| Language | Docker Image | Test Command |
|----------|-------------|--------------|
| Python | `python:3.12-slim` | `python -m pytest -v` |
| JavaScript/TypeScript | `node:22-slim` | `npm test` |
| Go | `golang:1.23-bookworm` | `go test -v ./...` |
| Rust | `rust:1.82-slim-bookworm` | `cargo test` |
| Java (Maven) | `maven:3.9-eclipse-temurin-21` | `mvn test -q` |
| C# / .NET | `mcr.microsoft.com/dotnet/sdk:9.0` | `dotnet test --verbosity normal` |
| Ruby | `ruby:3.3-slim` | `bundle exec rspec` |
| PHP | `composer:2` | `vendor/bin/phpunit` |
| C/C++ | `gcc:14` | CMake build + `ctest` (`make test` for a Makefile) |
| PowerShell | `mcr.microsoft.com/powershell:7.4-ubuntu-22.04` | `Invoke-Pester -CI` |
| And more... | Auto-detected | Auto-configured |

The project's detected language wins when it has a toolchain rule; otherwise the patch's files decide by majority, then the workspace's marker files. Scripts and Makefiles never outvote a primary language. `pipeline.docker_image` / `--docker-image` / `DELEGATRON_DOCKER_IMAGE` sets the image explicitly (never switched per patch).

### Prompt Versions for Local Models

Every local preset (`local`, `local-phi4`, `local-heavy`, `local-oss`, `local-oss-review`) pins `prompt_version = 5` for the architect, implementer and reviewer. A role that leaves `prompt_version` at 0 (unset) is resolved by its endpoint in `factories/agent_factory.resolve_prompt_version`: v5 for a local endpoint or the mock provider, v1 for a cloud API. The director always uses its own `prompts/director/v1.md`. Key features of v5:

- Used with phi4:14b and gpt-oss:20b (architect/reviewer) and qwen2.5-coder:14b, gpt-oss:20b, phi4:14b and qwen3-coder:30b (implementer)
- Fix cycles start from the evidence: "Failing Tests" are ground truth, only one side of a failing assertion (the code or the test) changes per attempt, and reviewer notes and import warnings are advice ranked below the tests
- The implementer returns only the files it creates or changes (files left out are kept) and deletes files through `delete_files`
- Two-pass mode: code first, then tests that import what the code really defines
- Structured JSON output constraints and sandbox environment warnings (no network access, mock HTTP calls)

v4 is kept unchanged as the A/B baseline: pin `prompt_version = 4` on a role to compare. The `single-claude` and `single-gpt` presets still pin architect v4.

### SWE-bench Evaluation (`swebench.py`)

Integration with [SWE-bench](https://www.swebench.com/), the standard benchmark for evaluating automated software engineering tools on real GitHub issues:

- Runs Delegatron against actual GitHub issue descriptions
- Supports the SWE-bench Lite, Verified and full datasets (`--split lite|verified|full`; needs the `swebench` extra)
- Filters by complexity tier: simple, moderate, complex, feature
- Reports resolution rates and cost per instance
- Accessible via `delegatron swebench` CLI and `POST /api/swebench` endpoint

### Multi-Milestone Project Mode (`project.py`)

Orchestrates large requests that span multiple pipeline runs:

1. A meta-architect (`prompts/meta_architect/v1.md`, on the architect's model) decomposes the high-level request into milestones
2. Each milestone runs through the full pipeline (plan/code/test/review)
3. Code accumulates across milestones in the workspace
4. Global cost and time budgets are enforced across all milestones
5. Accessible via `delegatron project` CLI and `POST /api/projects` endpoint

### Repository Mapping (`repomap.py`)

Generates a significance-scored map of repository files to help agents prioritize code review and understand project structure. Used internally by context gathering and review flows.

### Other Modules

- **`topology.py`**: Graph-based topology analysis for multi-step pipelines
- **`routing.py`**: Advanced routing logic beyond the basic router
- **`queue.py`**: Task queue management for async pipeline execution
- **`ratelimit.py`**: Rate limiting for API endpoints
- **`multirepo.py`**: Multi-repository orchestration support
- **`worktree.py`**: Git worktree management; the pipeline generates the first patches of ready tasks in worktrees of the run branch when `parallel_tasks` is on
- **`integrations/git_pr.py`**: GitHub PR creation after an approved run (`create_pr` / `--create-pr`, needs the `gh` CLI; a PR failure never fails the run)
- **`audit.py`**: Audit logging for compliance and debugging
- **`server/run_workspace.py`**: Server run workspaces (`.delegatron-runs/<run_id>`): creation, lookup, removal, and the files a run produced
- **`repo_resolve.py`**: Resolve workspace args — local path or git URL (GitHub, GitLab, Bitbucket, SSH) with automatic clone (`--branch` picks the branch)
- **`framework_hints.py`**: Tells the implementer the project's real test framework and installed dependencies
- **`orchestrator/evidence.py`**: Evidence derived for the agents (the attempt's diff for the reviewer, flaky tests from the test history, entrypoints to smoke-run)
- **`orchestrator/suite_guard.py`**: Test-suite guards for the completion gate — recognise test files, protect accepted tests, excuse known failures
- **`orchestrator/plan_normalize.py`**: Makes architect plans consistent before execution
- **`orchestrator/observer.py`**: Passive stuck-pattern detection and per-step metrics (`observer_metrics` in the summary)
- **`agents/rendering.py`**: Markdown building blocks for the agents' user messages
- **`utils/test_output.py`**: The shared test-output parser — per-test outcomes, failure excerpts, failure signatures
- **`utils/context_budget.py`**: Keeps a rendered prompt inside the model's context window
- **`utils/paths.py`**: Path normalization for model-supplied paths and Delegatron's internal directories (`runs`, `_sandbox`, `reviews`, `.delegatron-runs`, `.delegatron-worktrees`, `.delegatron-venv`), which are never committed, copied into sandboxes or shown to agents

---

## 12. CLI

The CLI (`src/delegatron/cli.py`) uses Click with Rich for formatting.

### Commands

| Command | Description |
|---------|-------------|
| `delegatron run "request"` | Full plan/code/test/review loop |
| `delegatron plan "request"` | Generate architect plan only (no execution) |
| `delegatron review` | Read-only code review or log analysis (no changes) |
| `delegatron resume <run_id>` | Resume from last checkpoint |
| `delegatron refine <run_id>` | Interactive post-pipeline refinement loop |
| `delegatron retry <run_id>` | Smart retry: re-run with the unresolved review issues appended (`--fresh` for a clean restart) |
| `delegatron show <run_id>` | Display run summary and artifacts |
| `delegatron list-runs` | List all pipeline runs |
| `delegatron estimate "request"` | Estimate cost before executing |
| `delegatron list-presets` | Show available presets |
| `delegatron benchmark "request"` | Compare presets on same task |
| `delegatron swebench` | Evaluate on SWE-bench GitHub issues |
| `delegatron project "request"` | Multi-milestone project orchestration |
| `delegatron serve` | Start the API server (uvicorn) |
| `delegatron template list` | List saved templates |
| `delegatron template save <name>` | Save a reusable template |
| `delegatron template run <name>` | Run from a saved template |
| `delegatron template delete <name>` | Delete a template |

### Common Flags

- `--preset` — Config preset (`default`, `claude-heavy`, `budget`, `mock`, `local`, `local-phi4`, `local-heavy`, `local-oss`, `local-oss-review`, `single-claude`, `single-gpt`)
- `--config` — Path to `delegatron.toml`
- `--workspace` — Target repository path (`run` and `review` also accept a git URL, cloned first; `--branch` picks the branch to clone)
- `--max-iterations`, `--max-cost`, `--max-time` — Override budget limits
- `--sandbox` — `docker`, `e2b`, `local` or `none` (alias of `local`)
- `--docker-image` — Docker image for the sandbox (e.g. `node:22-slim`)
- `--interactive` — Approve plan before coding
- `--mock` — Use mock providers (no API keys needed)
- `--auto` — Auto-select preset based on task complexity and project characteristics
- `--stream` — Stream LLM token output to stderr in real time
- `--create-pr`, `--pr-base`, `--pr-draft` — Open a GitHub PR when the run is approved (needs the `gh` CLI)
- `--allow-dirty` — Start even if the git workspace has uncommitted changes (`run`, `resume`, `refine`, `retry`, `benchmark`, `project`, `template run`)
- `--log-file` — Write JSON logs to a file
- `--json-output` — Machine-readable output
- `-v`, `--verbose` — Debug logging

Flags vary per command; `delegatron <command> --help` lists them.

---

## 13. Dashboard

A Next.js web dashboard (`dashboard/`) provides a visual interface. Pages are organized as Next.js App Router routes under `dashboard/src/app/`:

| Page | Route | Description |
|------|-------|-------------|
| **Home** | `/` | Landing / overview with system health widget (Server, Ollama, Docker, Disk) |
| **New Run** | `/new` | Full configuration UI — task description, preset selection, Docker image dropdown, role overrides, workspace path, sandbox type, max iterations/cost, interactive mode toggle, constraints |
| **Runs** | `/runs` | Browse runs with checkbox selection, bulk delete, active run indicator in sidebar |
| **Run Detail** | `/runs/[id]` | Live SSE events with planning progress bar, agent reasoning, streaming tokens, sparkline charts (Ollama VRAM, running Docker containers with per-container CPU/memory), code viewer (tabbed files), sandbox info panel, plan approval UI, Retry/Resume buttons |
| **Review** | `/review` | Read-only code review and log file analysis |
| **Compare** | `/compare` | Side-by-side metrics table for any two runs with color-coded diffs |
| **Compliance** | `/compliance` | Zero-cloud verification with green/red verdict, event log, provider breakdown, fingerprint, JSON export |
| **Templates** | `/templates` | 30 built-in + custom templates with variable substitution |
| **Projects** | `/projects` | Start multi-milestone projects and follow their milestones |
| **Analytics** | `/analytics` | Total runs and spend, average cost per run, success rate, runs by status, preset performance table (approval/error rates) |
| **Settings** | `/settings` | API endpoint, default preset (LOCAL badges for Ollama presets), pipeline defaults, log level and timezone, Ollama restart, experimental features, alerting, API key status, authentication key, settings export/import. The dashboard is dark-themed only |

Communicates with the Delegatron API server. On Windows, `start-dashboard.bat` provides a one-click launcher for both API server and dashboard. The dashboard has no benchmark page (removed 2026-04-16); benchmarks run from `delegatron benchmark` or `POST /api/benchmark`.

---

## 14. Deployment

### Docker

`Dockerfile` builds the server image (multi-stage, non-root user `delegatron`, `git` installed, the `cloud-providers` extra by default via the `EXTRAS` build arg). It runs `uvicorn delegatron.server.app:app` on port 8000 and configures the server from `DELEGATRON_WORKSPACE=/app/workspace`, `DELEGATRON_ARTIFACTS_DIR=/app/runs` and `DELEGATRON_DATA_DIR=/app/data`. `docker-compose.yml` provides a complete stack:
- Delegatron API server
- Workspace volume mount (`./workspace`)
- Run artifacts volume (`./runs`)
- Data volume for SQLite (`./data`)

The image has no Docker CLI: use the e2b sandbox, or give the container Docker access (see `docs/deployment.md`) for the default docker sandbox.

### Kubernetes / Helm

`helm/` chart with deployment, service, and configurable values:
- Resource limits, replica count (keep it at 1: the server holds active runs, SSE streams and plan approvals in memory)
- Environment variables for API keys (a Secret) and the run limits, sandbox and log level (a ConfigMap)
- Liveness and readiness probes on `/api/health`
- No volume claims: the workspace, run artifacts and SQLite data live in the container's filesystem and are lost when the pod restarts

### Raspberry Pi

Dedicated guide at `docs/deployment/raspberry-pi.md` for self-hosted deployment on ARM hardware.

---

## 15. Key Design Decisions

### Why Structured Schemas Instead of Chat

Agents communicate exclusively via validated JSON artifacts, not free-form text. This gives:
- Deterministic parsing (no regex extraction from prose)
- Schema evolution with backward compatibility
- Machine-readable audit trail
- Easier testing (mock providers return schema-valid canned responses)

### Why the Orchestrator Owns All State

Models are advisory — only the orchestrator mutates the repo, runs code, or makes routing decisions. This ensures:
- Models can't bypass budget guardrails
- All side effects are logged and auditable
- Models can be swapped without changing control flow

### Why Stateless Agents (Context Reset Each Iteration)

- Prevents context window bloat over many iterations
- Each step is independently retryable
- Eliminates position-bias artifacts from long conversations
- Smaller, focused prompts produce better results

### Why Docker as Default Sandbox

- Available on any developer machine
- No cloud dependency or API key needed
- Network isolation (`--network none`), memory limits, CPU limits
- E2B available as an upgrade path for production workloads

---

## 16. Known Limitations

### Current (as of October 2026)

1. **Mostly sequential execution**: With `parallel_tasks`, only the first patches of the ready tasks are generated concurrently (in git worktrees); applying, testing, reviewing and gating stay sequential, in plan order. The server runs one pipeline at a time and queues the rest.

2. **Webhook limitations**: GitHub webhook-triggered runs are disabled when auth is enabled (no repo→team binding). Comment-based triggering only.

3. **Diff application**: The sandbox can apply new-file diffs directly but falls back to the `patch` command for modifications to existing files. Complex diffs (renames, binary files) may not apply cleanly.

4. **Not wired yet**: the plugin system (`plugins.py`) is never loaded by the pipeline, CLI or server; `analytics.AnalyticsStore` is not written to.

### Architecture Invariants to Preserve

- Orchestrator is the sole mutator of repo and state
- All inter-agent data flows through schema-validated artifacts
- Budget guardrails are checked before and during every iteration
- No agent-generated code ever runs on the host (sandbox only; the opt-in `local` sandbox is the explicit exception)
- All operations must be idempotent and safely retryable
