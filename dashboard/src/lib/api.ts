// Default: infer API URL from the dashboard's origin.
// The API server always runs on (dashboard_port - 1).
// Override via NEXT_PUBLIC_API_URL env var or localStorage in Settings.

function inferApiUrl(): string {
  if (typeof window !== "undefined") {
    const port = parseInt(window.location.port, 10);
    if (port > 1) {
      return `${window.location.protocol}//${window.location.hostname}:${port - 1}`;
    }
  }
  return "";
}

export function getApiUrl(): string {
  if (typeof window !== "undefined") {
    const stored = localStorage.getItem("delegatron_api_url");
    if (stored) return stored;
  }
  return process.env.NEXT_PUBLIC_API_URL || inferApiUrl();
}

function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("delegatron_api_key");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
  }
  return headers;
}

export interface RunSummary {
  run_id: string;
  status: string;
  iterations?: number;
  total_cost_usd?: number;
  elapsed_seconds?: number;
  request?: string;
  plan_summary?: string;
  is_approved?: boolean;
  error?: string;
  created_at?: string;
}

export interface UsageSummary {
  total_runs: number;
  total_cost_usd: number;
  avg_cost_per_run: number;
  avg_iterations: number;
  success_rate: number;
  most_used_preset: string;
  runs_by_status: Record<string, number>;
  cost_by_model: Record<string, number>;
}

export interface HealthResponse {
  status: string;
  version: string;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const base = getApiUrl();
  const authHeaders = getAuthHeaders();
  const mergedHeaders: Record<string, string> = {
    ...authHeaders,
    ...(init?.headers as Record<string, string>),
  };
  const url = `${base}${path}`;
  let res: Response;
  try {
    res = await fetch(url, { ...init, headers: mergedHeaders });
  } catch (e) {
    // Network-level failure: DNS, CORS, server unreachable, etc.
    const msg = e instanceof Error ? e.message : String(e);
    throw new Error(`Cannot reach API at ${url} — ${msg}. Check that the server is running (default http://localhost:8000) and clear delegatron_api_url in Settings if set.`);
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await errorDetail(res)} (${url})`);
  }
  return res.json();
}

// The server's explanation of a failed request (FastAPI `detail`), else the status text.
async function errorDetail(res: Response): Promise<string> {
  try {
    const detail = (await res.json())?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d: { msg?: unknown }) => (typeof d?.msg === "string" ? d.msg : JSON.stringify(d)))
        .join("; ");
    }
  } catch {
    // Not a JSON body
  }
  return res.statusText;
}

export async function listRuns(): Promise<RunSummary[]> {
  return apiFetch<RunSummary[]>("/api/runs");
}

export async function getRun(id: string): Promise<RunSummary> {
  return apiFetch<RunSummary>(`/api/runs/${id}`);
}

export async function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/api/health");
}

export async function getAnalytics(): Promise<UsageSummary> {
  return apiFetch<UsageSummary>("/api/analytics");
}

export async function deleteRun(id: string): Promise<{ status: string }> {
  return apiFetch(`/api/runs/${id}`, { method: "DELETE" });
}

export async function cancelRun(id: string): Promise<{ status: string }> {
  return apiFetch(`/api/runs/${id}/cancel`, { method: "POST" });
}

export async function approveRun(id: string): Promise<unknown> {
  return apiFetch(`/api/runs/${id}/approve`, { method: "POST" });
}

export async function rejectRun(id: string): Promise<unknown> {
  return apiFetch(`/api/runs/${id}/reject`, { method: "POST" });
}

export async function rerunRun(id: string): Promise<{ run_id: string; status: string; message: string }> {
  return apiFetch(`/api/runs/${id}/rerun`, { method: "POST" });
}

export async function createRun(body: Record<string, unknown>): Promise<{ run_id: string; status: string; message: string }> {
  return apiFetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getLeaderboard(): Promise<Record<string, unknown>[]> {
  return apiFetch("/api/analytics/leaderboard");
}

export async function setLogLevel(level: string): Promise<void> {
  await apiFetch("/api/settings/log-level", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level }),
  });
}

export async function setLogTimezone(timezone: string): Promise<void> {
  await apiFetch("/api/settings/log-timezone", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone }),
  });
}

/**
 * Get a signed URL for SSE/download endpoints.
 *
 * Calls GET /api/stream-token to obtain a short-lived HMAC-signed URL,
 * avoiding the need to put long-lived API keys in query strings where
 * they leak into browser history, proxy logs, and analytics.
 *
 * Falls back to unsigned URL if no API key is configured (auth disabled).
 */
export async function getAuthenticatedSSEUrl(path: string): Promise<string> {
  const base = getApiUrl();
  const token = typeof window !== "undefined" ? localStorage.getItem("delegatron_api_key") : null;

  if (!token) {
    // No auth configured — use plain URL
    return `${base}${path}`;
  }

  try {
    const resp = await fetch(
      `${base}/api/stream-token?path=${encodeURIComponent(path)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (resp.ok) {
      const data = await resp.json();
      return `${base}${data.url}`;
    }
  } catch {
    // Fall through to unsigned URL
  }

  // Fallback: unsigned URL (server may reject if auth is required)
  return `${base}${path}`;
}

export async function getSystemStats(): Promise<Record<string, unknown>> {
  return apiFetch("/api/system/stats");
}

export interface RoleOverride {
  provider?: string;
  model?: string;
  base_url?: string;
  api_key_env?: string;
  max_tokens?: number;
  temperature?: number;
}

export interface ConstraintsConfig {
  no_modify?: string[];
  /** Literal text, or `re:<regex>`. */
  required_patterns?: string[];
  /** Literal text, or `re:<regex>`. */
  forbidden_patterns?: string[];
  /** Prose rules shown to the agents; never pattern-checked. */
  guidelines?: string[];
}

export interface CreateRunRequest {
  request: string;
  preset?: string;
  workspace?: string;
  sandbox?: string;
  max_iterations?: number;
  max_cost_usd?: number;
  max_wall_time_minutes?: number;
  interactive?: boolean;
  /** Open a GitHub PR on approved runs (requires `gh` CLI authenticated). */
  create_pr?: boolean;
  /** Base branch for the PR (default: main). */
  pr_base?: string;
  /** Open the PR as a draft. */
  pr_draft?: boolean;
  architect?: RoleOverride;
  implementer?: RoleOverride;
  reviewer?: RoleOverride;
  constraints?: ConstraintsConfig;
}

// --- Plan Only ---

export interface PlanResponse {
  run_id: string;
  status: string;
  plan_summary: string;
  tasks: Record<string, unknown>[];
  assumptions: string[];
  cost_usd: number;
  plan: Record<string, unknown>;
}

export async function planOnly(body: CreateRunRequest): Promise<PlanResponse> {
  return apiFetch<PlanResponse>("/api/runs/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// --- Cost Estimate ---

export interface EstimateRequest {
  preset?: string;
  architect?: RoleOverride;
  implementer?: RoleOverride;
  reviewer?: RoleOverride;
  max_iterations?: number;
}

export interface EstimateResponse {
  min_cost_usd: number;
  max_cost_usd: number;
  breakdown: Record<string, unknown>;
  models: Record<string, string>;
}

export async function estimateCost(body: EstimateRequest): Promise<EstimateResponse> {
  return apiFetch<EstimateResponse>("/api/estimate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// --- Templates ---

export interface Template {
  name: string;
  description: string;
  request_pattern: string;
  preset?: string;
  overrides?: Record<string, unknown>;
  constraints?: Record<string, unknown>;
  variables?: Record<string, string>;
  /** True when shipped with Delegatron; user can't delete these. */
  is_builtin?: boolean;
}

export async function listTemplates(): Promise<Template[]> {
  return apiFetch<Template[]>("/api/templates");
}

export async function createTemplate(body: Template): Promise<{ status: string; name: string }> {
  return apiFetch("/api/templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteTemplate(name: string): Promise<{ status: string }> {
  return apiFetch(`/api/templates/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function runTemplate(name: string, variables?: Record<string, string>): Promise<{ run_id: string; status: string; message: string }> {
  return apiFetch(`/api/templates/${encodeURIComponent(name)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ variables: variables || {} }),
  });
}

// --- Benchmark ---

export interface BenchmarkResultItem {
  preset: string;
  status: string;
  iterations: number;
  cost_usd: number;
  elapsed_seconds: number;
  approved: boolean;
  error: string;
}

export interface BenchmarkResponse {
  benchmark_id: string;
  status: string; // "running" | "complete"
  request: string;
  presets: string[];
  results: BenchmarkResultItem[];
  cheapest_passing: string | null;
}

export interface StartBenchmarkRequest {
  request: string;
  presets: string[];
  max_iterations?: number;
  sandbox?: string;
}

export async function startBenchmark(body: StartBenchmarkRequest): Promise<{ benchmark_id: string; status: string; message: string }> {
  return apiFetch("/api/benchmark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getBenchmark(id: string): Promise<BenchmarkResponse> {
  return apiFetch<BenchmarkResponse>(`/api/benchmark/${id}`);
}

// --- SWE-bench ---

export interface SWEBenchTierData {
  total: number;
  resolved: number;
  resolve_rate: number;
  avg_cost: number;
  avg_iterations: number;
}

export interface SWEBenchResultItem {
  instance_id: string;
  tier: string;
  resolved: boolean;
  status: string;
  iterations: number;
  cost_usd: number;
  elapsed_seconds: number;
  emcs_score: number;
  error: string;
}

export interface SWEBenchEval {
  eval_id: string;
  status: string;
  preset: string;
  split: string;
  total_instances: number;
  completed?: number;
  results: SWEBenchResultItem[];
  resolve_rate: number;
  total_cost_usd: number;
  total_time_seconds?: number;
  avg_iterations?: number;
  by_tier: Record<string, SWEBenchTierData>;
  error?: string;
}

export interface SWEBenchListItem {
  eval_id: string;
  status: string;
  preset: string;
  split: string;
  total_instances: number;
  completed: number;
  resolve_rate: number;
  total_cost_usd: number;
}

export async function startSWEBench(body: {
  preset: string;
  split: string;
  instances: number;
  tier?: string;
  max_iterations?: number;
  max_cost_usd?: number;
}): Promise<{ eval_id: string; status: string; message: string }> {
  return apiFetch("/api/swebench", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getSWEBench(id: string): Promise<SWEBenchEval> {
  return apiFetch<SWEBenchEval>(`/api/swebench/${id}`);
}

export async function listSWEBench(): Promise<SWEBenchListItem[]> {
  return apiFetch<SWEBenchListItem[]>("/api/swebench");
}

// --- Resume ---

// New limits for the continued run; omitted ones keep the run's settings.
export interface ResumeLimits {
  max_iterations?: number;
  max_wall_time_minutes?: number;
  max_cost_usd?: number;
}

// Continues the run under the same run_id, in its own workspace.
export async function resumeRun(
  id: string,
  limits?: ResumeLimits,
): Promise<{ run_id: string; status: string; message: string }> {
  const init: RequestInit = { method: "POST" };
  if (limits && Object.keys(limits).length > 0) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(limits);
  }
  return apiFetch(`/api/runs/${id}/resume`, init);
}

export async function refineRun(id: string): Promise<{ run_id: string; status: string; message: string }> {
  return apiFetch(`/api/runs/${id}/refine`, { method: "POST" });
}

export interface RetryResponse {
  run_id: string;
  status: string;
  message: string;
  strategy: "rerun" | "refine";
}

export async function retryRun(
  id: string,
  opts?: { freshStart?: boolean; preset?: string },
): Promise<RetryResponse> {
  const body: Record<string, unknown> = {
    fresh_start: opts?.freshStart ?? false,
  };
  if (opts?.preset) body.preset = opts.preset;
  return apiFetch<RetryResponse>(`/api/runs/${id}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// --- Provider key status ---

export async function getProviderKeyStatus(): Promise<Record<string, boolean>> {
  return apiFetch<Record<string, boolean>>("/api/provider-keys");
}

// --- Server defaults (from .env) ---

export interface ServerDefaults {
  sandbox: string;
  max_iterations: number;
  max_cost_usd: number;
  max_wall_time_minutes: number;
  log_level: string;
  log_timezone: string;
  workspace: string;
  auth_enabled: boolean;
  provider_keys: Record<string, boolean>;
  parallel_tasks: boolean;
  project_mode: boolean;
}

export async function getServerDefaults(): Promise<ServerDefaults> {
  return apiFetch<ServerDefaults>("/api/defaults");
}

// --- Feature flag toggles ---

export async function setParallelTasks(enabled: boolean): Promise<void> {
  await apiFetch("/api/settings/parallel-tasks", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function setProjectMode(enabled: boolean): Promise<void> {
  await apiFetch("/api/settings/project-mode", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// --- Project mode ---

export interface ProjectMilestoneResult {
  milestone_id: string;
  title: string;
  status: string;
  iterations: number;
  cost_usd: number;
  elapsed_seconds: number;
  emcs_score: number;
  files_created: string[];
  error: string;
}

export interface ProjectData {
  project_id: string;
  status: string;
  request: string;
  preset: string;
  plan: { summary: string; milestones: Array<{ id: string; title: string; description: string }> } | null;
  results: ProjectMilestoneResult[];
  milestones_completed: number;
  milestones_total: number;
  total_cost_usd: number;
  total_time_seconds?: number;
  error?: string;
}

export interface ProjectListItem {
  project_id: string;
  status: string;
  request: string;
  preset: string;
  milestones_completed: number;
  milestones_total: number;
  total_cost_usd: number;
}

export async function createProject(body: {
  request: string;
  preset?: string;
  max_milestones?: number;
  max_iterations?: number;
  max_cost_usd?: number;
  max_wall_time_minutes?: number;
}): Promise<{ project_id: string; status: string; message: string }> {
  return apiFetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getProject(id: string): Promise<ProjectData> {
  return apiFetch<ProjectData>(`/api/projects/${id}`);
}

export async function listProjects(): Promise<ProjectListItem[]> {
  return apiFetch<ProjectListItem[]>("/api/projects");
}

export function setApiUrl(url: string): void {
  localStorage.setItem("delegatron_api_url", url);
}

export function getStoredApiUrl(): string {
  return getApiUrl();
}

export function setApiKey(key: string): void {
  localStorage.setItem("delegatron_api_key", key);
}

export function clearApiKey(): void {
  localStorage.removeItem("delegatron_api_key");
}

export function hasApiKey(): boolean {
  if (typeof window === "undefined") return false;
  return !!localStorage.getItem("delegatron_api_key");
}

// --- Run Files (generated code) ---

export async function getRunFiles(runId: string): Promise<Record<string, string>> {
  const data = await apiFetch<{ files: Record<string, string> }>(`/api/runs/${runId}/files`);
  return data.files;
}

// --- Iteration History ---

export interface IterationTestSummary {
  passed: boolean;
  total: number;
  failures: number;
}

export interface IterationReviewSummary {
  reviewer_accepted: boolean;
  summary: string;
  issue_counts: Record<string, number>;
}

export interface IterationSummary {
  iteration: number;
  status: string;
  is_approved: boolean;
  patch_applied: boolean;
  total_cost_usd: number;
  files_changed?: string[];
  rationale?: string;
  tests?: IterationTestSummary;
  review?: IterationReviewSummary;
}

export interface IterationsResponse {
  run_id: string;
  iterations: IterationSummary[];
}

export interface IterationDiff {
  run_id: string;
  iteration: number;
  diff_unified: string;
  rationale: string;
  files: Record<string, string>;
  test_report?: { passed?: boolean; total?: number; failures?: number; stderr?: string; [key: string]: unknown };
  review_report?: { verdict?: string; issues?: Array<{ severity: string; message: string }>; [key: string]: unknown };
}

export async function getIterations(runId: string): Promise<IterationsResponse> {
  return apiFetch<IterationsResponse>(`/api/runs/${runId}/iterations`);
}

export async function getIterationDiff(runId: string, iteration: number): Promise<IterationDiff> {
  return apiFetch<IterationDiff>(`/api/runs/${runId}/iterations/${iteration}/diff`);
}

export async function restoreIteration(runId: string, iteration: number): Promise<{ status: string; restored_to_iteration: number }> {
  return apiFetch(`/api/runs/${runId}/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ iteration }),
  });
}

// --- Ollama Status ---

export interface OllamaStatus {
  running: boolean;
  models: string[];
  url: string;
}

// --- System Health ---

export interface SystemHealth {
  status: string;
  version: string;
  server: { active_runs: number; auth_enabled: boolean };
  ollama: OllamaStatus;
  docker: { available: boolean; version: string };
  disk: { free_gb: number; total_gb: number; used_percent: number };
  providers: Record<string, boolean>;
}

export async function getSystemHealth(): Promise<SystemHealth> {
  return apiFetch<SystemHealth>("/api/health/full");
}

export async function getOllamaStatus(): Promise<OllamaStatus> {
  const defaults = await getServerDefaults();
  return (defaults as any).ollama || { running: false, models: [], url: "" };
}

export async function restartOllama(): Promise<{
  status: string;
  models_available: number;
  message: string;
}> {
  return apiFetch("/api/ollama/restart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
}

// --- Code Review (read-only) ---

export interface ReviewResponse {
  review_id: string;
  summary: string;
  accepted: boolean;
  issues: Array<{
    severity: string;
    title: string;
    details: string;
    recommended_fix?: string;
  }>;
  files_reviewed: number;
  cost_usd: number;
  project: Record<string, unknown>;
  suggestions?: Record<string, unknown>;
}

export async function reviewCodebase(body: {
  workspace?: string;
  focus?: string;
  with_suggestions?: boolean;
  preset?: string;
}): Promise<ReviewResponse> {
  return apiFetch<ReviewResponse>("/api/review", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// --- Compliance / Zero-Cloud Verification ---

export interface ComplianceReport {
  run_id: string;
  cloud_free: boolean;
  timestamp: string;
  total_events: number;
  violations: Array<{
    timestamp: string;
    category: string;
    role: string;
    provider: string;
    host: string;
    reason: string;
  }>;
  summary: {
    total_events: number;
    local_events: number;
    cloud_events: number;
    by_category: Record<string, number>;
    by_provider: Record<string, number>;
    enforcement_mode: string;
  };
  events: Array<{
    timestamp: string;
    category: string;
    role: string;
    provider: string;
    host: string;
    is_local: boolean;
    details: Record<string, unknown>;
  }>;
  fingerprint: string;
}

export async function getComplianceReport(runId: string): Promise<ComplianceReport> {
  return apiFetch<ComplianceReport>(`/api/runs/${runId}/compliance`);
}
