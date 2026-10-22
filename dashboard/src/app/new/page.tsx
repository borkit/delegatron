"use client";

import { useState, useEffect, useCallback, useRef, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { createTemplate, createRun, planOnly, estimateCost, getServerDefaults, getApiUrl, type PlanResponse, type EstimateResponse, type ServerDefaults } from "@/lib/api";
import { FALLBACK_DEFAULTS, migrateLegacyDefaults, resolveFormDefaults, type PipelineDefaults } from "@/lib/pipelineDefaults";

// Which API keys each preset requires
const PRESET_REQUIRED_KEYS: Record<string, string[]> = {
  default: ["anthropic", "openai", "gemini"],
  "claude-heavy": ["anthropic", "gemini"],
  budget: ["gemini"],
  local: [],
  "local-phi4": [],
  "local-heavy": [],
  "local-oss": [],
  "local-oss-review": [],
  "single-claude": ["anthropic"],
  "single-gpt": ["openai"],
  mock: [],
};

const PRESET_GROUPS = [
  {
    title: "Local / Ollama",
    badge: "LOCAL",
    badgeColor: "bg-green-900/50 text-green-400 border-green-800",
    presets: [
      { value: "local-oss", label: "Local OSS ★", desc: "phi4 + gpt-oss:20b + phi4 — best local quality (validated 2026-04-18: only local model to pass fib + React in both roles). Fits in 12 GB VRAM." },
      { value: "local", label: "Local (default)", desc: "phi4 + qwen2.5-coder:14b + phi4 — 12 GB VRAM baseline, passes fib + React cleanly." },
      { value: "local-oss-review", label: "Local OSS Review", desc: "phi4 + qwen2.5-coder:14b + gpt-oss:20b — strongest local review (trade ~2× reviewer latency for tougher architectural critique)." },
      { value: "local-phi4", label: "Local Phi-4", desc: "phi4:14b all roles — fastest local option for simple tasks." },
      { value: "local-heavy", label: "Local Heavy ⚠️", desc: "phi4 + qwen3-coder:30b + phi4 — REQUIRES 24 GB+ VRAM (spills 30 GB to RAM on smaller cards)." },
    ],
  },
  {
    title: "Cloud",
    badge: "CLOUD",
    badgeColor: "bg-blue-900/50 text-blue-400 border-blue-800",
    presets: [
      { value: "default", label: "Cloud Trio", desc: "Claude + GPT + Gemini" },
      { value: "claude-heavy", label: "Claude Heavy", desc: "Claude architect + implementer" },
      { value: "budget", label: "Budget", desc: "All Gemini Flash" },
      { value: "single-claude", label: "Single Claude", desc: "All Claude Sonnet (baseline)" },
      { value: "single-gpt", label: "Single GPT", desc: "All GPT-4.1 (baseline)" },
    ],
  },
  {
    title: "Testing",
    badge: "TEST",
    badgeColor: "bg-gray-800 text-gray-400 border-gray-700",
    presets: [
      { value: "mock", label: "Mock", desc: "No API keys needed — for testing" },
    ],
  },
];

// Flat list for backward compat
const PRESETS = PRESET_GROUPS.flatMap((g) => g.presets);

const PROVIDERS = [
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "google", label: "Google" },
  { value: "openai-compatible", label: "OpenAI-Compatible (Ollama, Grok, etc.)" },
];

interface RoleOverride {
  provider?: string;
  model?: string;
  base_url?: string;
  api_key_env?: string;
  max_tokens?: number;
  temperature?: number;
}

function emptyRole(): RoleOverride {
  return { provider: "", model: "", base_url: "", api_key_env: "", max_tokens: undefined, temperature: undefined };
}

function isRoleEmpty(r: RoleOverride): boolean {
  return !r.provider && !r.model && !r.base_url && !r.api_key_env && r.max_tokens === undefined && r.temperature === undefined;
}

function cleanRole(r: RoleOverride): Record<string, unknown> | null {
  const out: Record<string, unknown> = {};
  if (r.provider) out.provider = r.provider;
  if (r.model) out.model = r.model;
  if (r.base_url) out.base_url = r.base_url;
  if (r.api_key_env) out.api_key_env = r.api_key_env;
  if (r.max_tokens !== undefined && r.max_tokens > 0) out.max_tokens = r.max_tokens;
  if (r.temperature !== undefined && r.temperature >= 0) out.temperature = r.temperature;
  return Object.keys(out).length > 0 ? out : null;
}

const PLAN_STEPS = [
  { label: "Loading workspace and detecting project", est: 3 },
  { label: "Analyzing task complexity", est: 5 },
  { label: "Running architect agent (local model)", est: 30 },
  { label: "Structuring tasks and acceptance criteria", est: 5 },
];

function PlanningProgress() {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef(Date.now());

  useEffect(() => {
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - startRef.current) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, []);

  let cumulative = 0;
  let currentStep = 0;
  for (let i = 0; i < PLAN_STEPS.length; i++) {
    cumulative += PLAN_STEPS[i].est;
    if (elapsed < cumulative) { currentStep = i; break; }
    if (i === PLAN_STEPS.length - 1) currentStep = i;
  }

  const totalEst = PLAN_STEPS.reduce((s, step) => s + step.est, 0);
  const pct = Math.min(95, Math.round((elapsed / totalEst) * 100));

  return (
    <div>
      <div className="space-y-2 mb-4">
        {PLAN_STEPS.map((step, i) => (
          <div key={i} className="flex items-center gap-3 text-sm">
            <span className={`w-5 h-5 flex items-center justify-center rounded-full text-xs font-bold ${
              i < currentStep ? "bg-green-600 text-white" :
              i === currentStep ? "bg-blue-600 text-white animate-pulse" :
              "bg-gray-800 text-gray-500"
            }`}>
              {i < currentStep ? "\u2713" : i + 1}
            </span>
            <span className={i <= currentStep ? "text-gray-200" : "text-gray-600"}>
              {step.label}
            </span>
            {i === currentStep && <span className="text-xs text-gray-500">~{step.est}s</span>}
          </div>
        ))}
      </div>
      <div className="flex items-center justify-between text-xs text-gray-500 mb-2">
        <span>{elapsed}s elapsed</span>
        <span>~{Math.max(0, totalEst - elapsed)}s remaining</span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div className="h-full bg-blue-600 rounded-full transition-all duration-1000" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

const DOCKER_IMAGES = [
  { value: "", label: "Auto-detect (Python 3.12)" },
  { value: "python:3.12-slim", label: "Python 3.12", group: "Language SDKs" },
  { value: "node:22-slim", label: "Node.js 22 (JS/TS)", group: "Language SDKs" },
  { value: "mcr.microsoft.com/dotnet/sdk:9.0", label: ".NET 9.0 (C#/F#)", group: "Language SDKs" },
  { value: "golang:1.24", label: "Go 1.24", group: "Language SDKs" },
  { value: "rust:1.86-slim", label: "Rust 1.86", group: "Language SDKs" },
  { value: "maven:3.9-eclipse-temurin-21", label: "Java 21 (Maven)", group: "Language SDKs" },
  { value: "gradle:8-jdk21", label: "Java 21 (Gradle)", group: "Language SDKs" },
  { value: "ruby:3.4-slim", label: "Ruby 3.4", group: "Language SDKs" },
  { value: "php:8.4-cli", label: "PHP 8.4", group: "Language SDKs" },
  { value: "mcr.microsoft.com/powershell:lts", label: "PowerShell 7 (Linux)", group: "Language SDKs" },
  { value: "mcr.microsoft.com/dotnet/sdk:9.0-nanoserver-ltsc2022", label: ".NET 9.0 Windows", group: "Windows" },
  { value: "mcr.microsoft.com/windows/servercore:ltsc2022", label: "Windows Server Core", group: "Windows" },
  { value: "mcr.microsoft.com/powershell:lts-nanoserver-ltsc2022", label: "PowerShell Windows", group: "Windows" },
];

function DockerImageSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const isKnown = DOCKER_IMAGES.some((i) => i.value === value);
  const [customMode, setCustomMode] = useState(!isKnown && value !== "");
  const [customValue, setCustomValue] = useState(isKnown ? "" : value);

  if (customMode) {
    return (
      <div className="flex gap-2">
        <input
          type="text"
          value={customValue}
          onChange={(e) => { setCustomValue(e.target.value); onChange(e.target.value); }}
          className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 font-mono"
          placeholder="myregistry/myimage:tag"
          autoFocus
        />
        <button
          type="button"
          onClick={() => { setCustomMode(false); onChange(""); }}
          className="px-3 py-2 text-xs bg-gray-700 text-gray-300 rounded-md hover:bg-gray-600"
        >
          Presets
        </button>
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <select
        value={value}
        onChange={(e) => {
          if (e.target.value === "__custom__") {
            setCustomMode(true);
            setCustomValue("");
            onChange("");
          } else {
            onChange(e.target.value);
          }
        }}
        className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
      >
        {DOCKER_IMAGES.filter((i) => !i.group).map((i) => (
          <option key={i.value} value={i.value}>{i.label}</option>
        ))}
        {["Language SDKs", "Windows"].map((g) => (
          <optgroup key={g} label={g}>
            {DOCKER_IMAGES.filter((i) => i.group === g).map((i) => (
              <option key={i.value} value={i.value}>{i.label}</option>
            ))}
          </optgroup>
        ))}
        <option value="__custom__">Custom image...</option>
      </select>
    </div>
  );
}

// Sandboxes an API caller may choose; any other server default (e.g. an
// operator's "local") applies when the request names none.
const API_SANDBOXES = ["docker", "e2b"];

function NewRunPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [request, setRequest] = useState(searchParams.get("request") || "");
  const [attachedFiles, setAttachedFiles] = useState<{ name: string; content: string }[]>([]);
  const [preset, setPreset] = useState(searchParams.get("preset") || "local");
  const [workspace, setWorkspace] = useState(FALLBACK_DEFAULTS.workspace);
  const [sandbox, setSandbox] = useState(FALLBACK_DEFAULTS.sandbox);
  const [dockerImage, setDockerImage] = useState(searchParams.get("docker_image") || "");
  const [maxIter, setMaxIter] = useState(FALLBACK_DEFAULTS.max_iterations);
  const [maxCost, setMaxCost] = useState(FALLBACK_DEFAULTS.max_cost_usd);
  const [maxWallTime, setMaxWallTime] = useState(FALLBACK_DEFAULTS.max_wall_time);
  const [interactive, setInteractive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [serverDefaults, setServerDefaults] = useState<ServerDefaults | null>(null);

  // Load defaults: localStorage (from Settings) > server (.env) > FALLBACK_DEFAULTS
  useEffect(() => {
    function apply(values: PipelineDefaults) {
      setMaxIter(values.max_iterations);
      setMaxCost(values.max_cost_usd);
      setMaxWallTime(values.max_wall_time);
      setSandbox(values.sandbox);
      setWorkspace(values.workspace);
    }

    migrateLegacyDefaults(localStorage);
    const lsPreset = localStorage.getItem("delegatron_preset");
    const lsImage = localStorage.getItem("delegatron_docker_image");
    if (!searchParams.get("preset") && lsPreset) setPreset(lsPreset);
    if (lsImage && !searchParams.get("docker_image")) setDockerImage(lsImage);
    apply(resolveFormDefaults(localStorage, null));

    // Then the server's defaults for every field this browser has not overridden
    getServerDefaults().then((defaults) => {
      setServerDefaults(defaults);
      apply(resolveFormDefaults(localStorage, defaults));
    }).catch(() => {});
  }, []);
  const [showSaveTemplate, setShowSaveTemplate] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [templateDesc, setTemplateDesc] = useState("");
  const [savingTemplate, setSavingTemplate] = useState(false);
  const [templateSaved, setTemplateSaved] = useState("");

  // Advanced: role overrides
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [architect, setArchitect] = useState<RoleOverride>(emptyRole());
  const [implementer, setImplementer] = useState<RoleOverride>(emptyRole());
  const [reviewer, setReviewer] = useState<RoleOverride>(emptyRole());

  // Plan-only mode
  const [planning, setPlanning] = useState(false);
  const [planResult, setPlanResult] = useState<PlanResponse | null>(null);

  // Cost estimate
  const [costEstimate, setCostEstimate] = useState<EstimateResponse | null>(null);

  // Constraints
  const [noModify, setNoModify] = useState("");
  const [requiredPatterns, setRequiredPatterns] = useState("");
  const [forbiddenPatterns, setForbiddenPatterns] = useState("");
  const [guidelines, setGuidelines] = useState("");

  function buildConstraints(): Record<string, string[]> | undefined {
    const constraints: Record<string, string[]> = {};
    const fields: [string, string][] = [
      ["no_modify", noModify],
      ["required_patterns", requiredPatterns],
      ["forbidden_patterns", forbiddenPatterns],
      ["guidelines", guidelines],
    ];
    for (const [key, text] of fields) {
      const lines = text.split("\n").map(s => s.trim()).filter(Boolean);
      if (lines.length) constraints[key] = lines;
    }
    return Object.keys(constraints).length > 0 ? constraints : undefined;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!request.trim()) return;
    setSubmitting(true);
    setError("");
    try {
      const body = buildRequestBody();
      const data = await createRun(body);
      router.push(`/runs/${data.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start run");
      setSubmitting(false);
    }
  }



  function buildRequestBody(): Record<string, unknown> {
    let fullRequest = request;
    if (attachedFiles.length > 0) {
      fullRequest += "\n\n--- ATTACHED FILES ---\n";
      for (const f of attachedFiles) {
        fullRequest += `\n### ${f.name}\n\`\`\`\n${f.content}\n\`\`\`\n`;
      }
    }
    const body: Record<string, unknown> = { request: fullRequest };
    if (preset) body.preset = preset;
    if (workspace.trim()) body.workspace = workspace.trim();
    if (API_SANDBOXES.includes(sandbox)) body.sandbox = sandbox;
    const effectiveImage = dockerImage.startsWith("custom:") ? dockerImage.slice(7) : dockerImage;
    if (effectiveImage.trim()) body.docker_image = effectiveImage.trim();
    if (maxIter) body.max_iterations = maxIter;
    if (maxCost) body.max_cost_usd = maxCost;
    if (maxWallTime) body.max_wall_time_minutes = maxWallTime;
    if (interactive) body.interactive = interactive;

    const arc = cleanRole(architect);
    const imp = cleanRole(implementer);
    const rev = cleanRole(reviewer);
    if (arc) body.architect = arc;
    if (imp) body.implementer = imp;
    if (rev) body.reviewer = rev;

    const constraints = buildConstraints();
    if (constraints) body.constraints = constraints;

    return body;
  }

  async function handlePlanOnly() {
    if (!request.trim()) return;
    setPlanning(true);
    setError("");
    setPlanResult(null);
    // Scroll to progress indicator
    setTimeout(() => {
      document.getElementById("plan-progress")?.scrollIntoView({ behavior: "smooth" });
    }, 100);
    try {
      const body = buildRequestBody();
      const result = await planOnly(body as unknown as Parameters<typeof planOnly>[0]);
      setPlanResult(result);
      // Scroll to plan result
      setTimeout(() => {
        document.getElementById("plan-result")?.scrollIntoView({ behavior: "smooth" });
      }, 100);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate plan");
    } finally {
      setPlanning(false);
    }
  }

  async function handleExecutePlan() {
    if (!request.trim()) return;
    setSubmitting(true);
    setError("");
    try {
      const body = buildRequestBody();
      // User already reviewed the plan — skip interactive approval
      body.interactive = false;
      const data = await createRun(body);
      setPlanResult(null);
      router.push(`/runs/${data.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start run");
      setSubmitting(false);
    }
  }

  // Live cost estimate: updates when preset or max_iterations changes
  const fetchEstimate = useCallback(async () => {
    try {
      const body: Parameters<typeof estimateCost>[0] = {
        preset: preset || undefined,
        max_iterations: maxIter,
      };
      const arc = cleanRole(architect);
      const imp = cleanRole(implementer);
      const rev = cleanRole(reviewer);
      if (arc) body.architect = arc as Parameters<typeof estimateCost>[0]["architect"];
      if (imp) body.implementer = imp as Parameters<typeof estimateCost>[0]["implementer"];
      if (rev) body.reviewer = rev as Parameters<typeof estimateCost>[0]["reviewer"];
      const result = await estimateCost(body);
      setCostEstimate(result);
    } catch {
      // Silently ignore estimate errors — non-critical
      setCostEstimate(null);
    }
  }, [preset, maxIter, architect, implementer, reviewer]);

  useEffect(() => {
    const timer = setTimeout(fetchEstimate, 300);
    return () => clearTimeout(timer);
  }, [fetchEstimate]);

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-bold text-white mb-2">New Run</h1>
      <p className="text-sm text-gray-500 mb-8">
        Describe what you want to build. The pipeline will architect, implement,
        test, and review automatically.
      </p>

      <form onSubmit={handleSubmit} className="space-y-6">
        <section>
          <label className="block text-sm font-medium text-gray-300 mb-2">
            Task Description
          </label>
          <textarea
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            rows={6}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 resize-y"
            placeholder={"Describe what you want built...\n\nExample: Create a GET /health endpoint in src/app/main.py that returns JSON with status and uptime. Add tests using TestClient."}
            maxLength={10000}
            required
          />
          <div className="flex items-center justify-between mt-2">
            <div className="flex items-center gap-2">
              <label className="cursor-pointer px-3 py-1.5 text-xs font-medium bg-gray-800 text-gray-400 border border-gray-700 rounded-lg hover:bg-gray-700 hover:text-gray-200 transition-colors">
                + Attach files
                <input
                  type="file"
                  multiple
                  accept=".py,.ps1,.sh,.bat,.txt,.md,.json,.toml,.yaml,.yml,.js,.ts,.css,.html,.cfg,.ini,.csv,.xml,.sql"
                  className="hidden"
                  onChange={async (e) => {
                    const files = Array.from(e.target.files || []);
                    const loaded: { name: string; content: string }[] = [];
                    for (const f of files) {
                      const text = await f.text();
                      loaded.push({ name: f.name, content: text });
                    }
                    setAttachedFiles((prev) => [...prev, ...loaded]);
                    e.target.value = "";
                  }}
                />
              </label>
              <span className="text-xs text-gray-600">
                Scripts, configs, text files — included as context for the AI
              </span>
            </div>
            <p className="text-xs text-gray-600">{request.length}/10000</p>
          </div>

          {attachedFiles.length > 0 && (
            <div className="mt-2 space-y-1">
              {attachedFiles.map((f, i) => (
                <div key={i} className="flex items-center justify-between bg-gray-800 rounded px-3 py-1.5 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="text-blue-400 font-mono">{f.name}</span>
                    <span className="text-gray-600">{f.content.split("\n").length} lines</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setAttachedFiles((prev) => prev.filter((_, j) => j !== i))}
                    className="text-red-400 hover:text-red-300"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        <section>
          <label className="block text-sm font-medium text-gray-300 mb-3">Provider Preset</label>
          <div className="space-y-4">
            <button
              type="button"
              onClick={() => setPreset("auto")}
              className={`w-full text-left rounded-lg p-3 border transition-colors ${
                preset === "auto"
                  ? "border-purple-600 bg-purple-600/10"
                  : "border-gray-800 bg-gray-900 hover:border-gray-700"
              }`}
              title="Automatically selects budget, default, or claude-heavy based on task complexity"
            >
              <span className="text-sm font-medium text-white">Auto-Select</span>
              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded border bg-purple-900/50 text-purple-400 border-purple-800 font-semibold">AUTO</span>
              <p className="text-xs text-gray-500 mt-1">Picks the best preset based on task complexity</p>
            </button>
            {PRESET_GROUPS.map((group) => (
              <div key={group.title}>
                <div className="flex items-center gap-2 mb-2">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-semibold ${group.badgeColor}`}>
                    {group.badge}
                  </span>
                  <span className="text-xs text-gray-500">{group.title}</span>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  {group.presets.map((p) => {
                    const requiredKeys = PRESET_REQUIRED_KEYS[p.value] || [];
                    const missingKeys = serverDefaults
                      ? requiredKeys.filter((k) => !serverDefaults.provider_keys[k])
                      : [];
                    const hasWarning = missingKeys.length > 0;

                    return (
                      <button
                        key={p.value}
                        type="button"
                        onClick={() => setPreset(p.value)}
                        className={`text-left rounded-lg p-3 border transition-colors ${
                          preset === p.value
                            ? "border-blue-600 bg-blue-600/10"
                            : hasWarning
                            ? "border-red-900/50 bg-gray-900 hover:border-red-800/50 opacity-70"
                            : "border-gray-800 bg-gray-900 hover:border-gray-700"
                        }`}
                      >
                        <span className="text-sm font-medium text-white">{p.label}</span>
                        <p className="text-xs text-gray-500 mt-1">{p.desc}</p>
                        {hasWarning && (
                          <p className="text-[10px] text-red-400 mt-1">
                            Missing: {missingKeys.join(", ")} key{missingKeys.length > 1 ? "s" : ""}
                          </p>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section>
          <label className="block text-sm font-medium text-gray-300 mb-3">Workspace</label>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Repository / Folder Path</label>
              <input
                type="text" value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                placeholder="/home/user/my-repo or https://github.com/org/repo.git"
              />
              <p className="text-xs text-gray-600 mt-1">Local path or git URL (GitHub, GitLab, Bitbucket, any git remote)</p>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-2">Sandbox</label>
              <div className="flex gap-2">
                {[
                  { value: "docker", label: "Docker", desc: "Local container" },
                  { value: "e2b", label: "E2B", desc: "Cloud microVM" },
                ].map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setSandbox(opt.value)}
                    className={`flex-1 text-left rounded-md px-3 py-2 border text-sm transition-colors ${
                      sandbox === opt.value
                        ? "border-blue-600 bg-blue-600/10 text-white"
                        : "border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600"
                    }`}
                  >
                    <span className="font-medium">{opt.label}</span>
                    <span className="text-xs text-gray-500 ml-2">{opt.desc}</span>
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-2">Docker Image</label>
              <select
                value={DOCKER_IMAGES.some(i => i.value === dockerImage) ? dockerImage : "__custom__"}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setDockerImage("custom:");
                  } else {
                    setDockerImage(e.target.value);
                  }
                }}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
              >
                {DOCKER_IMAGES.filter(i => !i.group).map(i => (
                  <option key={i.value} value={i.value}>{i.label}</option>
                ))}
                {["Language SDKs", "Windows"].map(g => (
                  <optgroup key={g} label={g}>
                    {DOCKER_IMAGES.filter(i => i.group === g).map(i => (
                      <option key={i.value} value={i.value}>{i.label}</option>
                    ))}
                  </optgroup>
                ))}
                <option value="__custom__">Custom image...</option>
              </select>
              {dockerImage.startsWith("custom:") && (
                <input
                  type="text"
                  value={dockerImage.replace("custom:", "")}
                  onChange={(e) => setDockerImage(e.target.value || "")}
                  className="w-full mt-2 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 font-mono"
                  placeholder="myregistry/myimage:tag"
                  autoFocus
                />
              )}
            </div>
          </div>
        </section>

        <section>
          <label className="block text-sm font-medium text-gray-300 mb-3">Configuration</label>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">Max Iterations</label>
                <input
                  type="number" value={maxIter}
                  onChange={(e) => setMaxIter(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
                  min={1} max={100}
                  className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Max Cost (USD)</label>
                <input
                  type="number" value={maxCost}
                  onChange={(e) => setMaxCost(Math.max(0, Math.min(1000, parseFloat(e.target.value) || 0)))}
                  min={0} max={1000} step={0.5}
                  className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">
                  Wall Time (min)
                  {serverDefaults && maxWallTime !== serverDefaults.max_wall_time_minutes && (
                    <span className="text-gray-600"> (server default: {serverDefaults.max_wall_time_minutes})</span>
                  )}
                </label>
                <input
                  type="number" value={maxWallTime}
                  onChange={(e) => setMaxWallTime(Math.max(1, Math.min(1440, parseInt(e.target.value) || 1)))}
                  min={1} max={1440}
                  className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                />
              </div>
            </div>
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox" checked={interactive}
                onChange={(e) => setInteractive(e.target.checked)}
                className="w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-600"
              />
              <div>
                <p className="text-sm text-white">Interactive mode</p>
                <p className="text-xs text-gray-500">Approve the plan before coding starts</p>
              </div>
            </label>

            {/* Cost Estimate */}
            {costEstimate && (
              <div className="bg-gray-800 border border-gray-700 rounded-md px-4 py-3">
                <p className="text-xs font-medium text-gray-400 mb-1">Estimated Cost</p>
                <p className="text-sm text-white">
                  ${costEstimate.min_cost_usd.toFixed(4)} &ndash; ${costEstimate.max_cost_usd.toFixed(4)} USD
                </p>
                <p className="text-xs text-gray-500 mt-1">
                  Models: {Object.entries(costEstimate.models).map(([role, model]) => (
                    <span key={role} className="inline-block mr-3">
                      <span className="text-gray-400">{role}:</span> {model}
                    </span>
                  ))}
                </p>
              </div>
            )}
          </div>
        </section>

        {/* Advanced: Role Overrides & Constraints */}
        <section>
          <button
            type="button"
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="flex items-center gap-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
          >
            <span className={`transition-transform ${showAdvanced ? "rotate-90" : ""}`}>&#9654;</span>
            Advanced Options
          </button>

          {showAdvanced && (
            <div className="mt-4 space-y-6">
              {/* Role Overrides */}
              <div>
                <p className="text-sm font-medium text-gray-300 mb-3">
                  Role Overrides
                  <span className="text-xs text-gray-600 ml-2 font-normal">Override preset models per role</span>
                </p>
                {([
                  { label: "Architect", state: architect, setter: setArchitect },
                  { label: "Implementer", state: implementer, setter: setImplementer },
                  { label: "Reviewer", state: reviewer, setter: setReviewer },
                ] as const).map(({ label, state, setter }) => (
                  <div key={label} className="bg-gray-900 border border-gray-800 rounded-lg p-4 mb-3">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">{label}</p>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Provider</label>
                        <select
                          value={state.provider || ""}
                          onChange={(e) => setter({ ...state, provider: e.target.value || undefined })}
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        >
                          <option value="">Use preset default</option>
                          {PROVIDERS.map((p) => (
                            <option key={p.value} value={p.value}>{p.label}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Model</label>
                        <input
                          type="text"
                          value={state.model || ""}
                          onChange={(e) => setter({ ...state, model: e.target.value || undefined })}
                          placeholder="e.g. qwen3-coder:30b"
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Base URL</label>
                        <input
                          type="text"
                          value={state.base_url || ""}
                          onChange={(e) => setter({ ...state, base_url: e.target.value || undefined })}
                          placeholder="http://localhost:11434/v1"
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">API Key Env Var</label>
                        <input
                          type="text"
                          value={state.api_key_env || ""}
                          onChange={(e) => setter({ ...state, api_key_env: e.target.value || undefined })}
                          placeholder="OPENAI_API_KEY"
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Max Tokens</label>
                        <input
                          type="number"
                          value={state.max_tokens ?? ""}
                          onChange={(e) => setter({ ...state, max_tokens: e.target.value ? parseInt(e.target.value) : undefined })}
                          placeholder="8192"
                          min={1} max={128000}
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        />
                      </div>
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Temperature</label>
                        <input
                          type="number"
                          value={state.temperature ?? ""}
                          onChange={(e) => setter({ ...state, temperature: e.target.value ? parseFloat(e.target.value) : undefined })}
                          placeholder="0.0"
                          min={0} max={2} step={0.1}
                          className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              {/* Constraints */}
              <div>
                <p className="text-sm font-medium text-gray-300 mb-3">
                  Constraints
                  <span className="text-xs text-gray-600 ml-2 font-normal">Rules the agents must follow</span>
                </p>
                <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-4">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">
                      No-Modify Files <span className="text-gray-700">(one path per line)</span>
                    </label>
                    <textarea
                      value={noModify}
                      onChange={(e) => setNoModify(e.target.value)}
                      rows={2}
                      placeholder={"alembic/versions/\nconfig/production.py\n.env"}
                      className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500 resize-y"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">
                      Required Patterns <span className="text-gray-700">(literal text or re:&lt;regex&gt;, one per line)</span>
                    </label>
                    <textarea
                      value={requiredPatterns}
                      onChange={(e) => setRequiredPatterns(e.target.value)}
                      rows={2}
                      placeholder={"def test_\nre:from pydantic import .*BaseModel"}
                      className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500 resize-y"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">
                      Forbidden Patterns <span className="text-gray-700">(literal text or re:&lt;regex&gt;, one per line)</span>
                    </label>
                    <textarea
                      value={forbiddenPatterns}
                      onChange={(e) => setForbiddenPatterns(e.target.value)}
                      rows={2}
                      placeholder={"eval(\nexec(\nre:os\\.system\\("}
                      className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500 resize-y"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">
                      Guidelines <span className="text-gray-700">(prose rules for the agents, one per line; not pattern-checked)</span>
                    </label>
                    <textarea
                      value={guidelines}
                      onChange={(e) => setGuidelines(e.target.value)}
                      rows={2}
                      placeholder="All endpoints must validate input with Pydantic models"
                      className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 resize-y"
                    />
                  </div>
                </div>
              </div>
            </div>
          )}
        </section>

        {error && (
          <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-400 text-sm">
            {error}
          </div>
        )}

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={submitting || planning || !request.trim()}
            className="flex-1 py-3 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? "Starting pipeline..." : "Start Run"}
          </button>
          <button
            type="button"
            onClick={handlePlanOnly}
            disabled={submitting || planning || !request.trim()}
            className="py-3 px-6 bg-gray-700 text-white font-medium rounded-lg hover:bg-gray-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed border border-gray-600"
          >
            {planning ? "Planning..." : "Plan Only"}
          </button>
          <button
            type="button"
            onClick={() => setShowSaveTemplate(!showSaveTemplate)}
            className="px-4 py-3 bg-gray-800 text-gray-400 font-medium rounded-lg border border-gray-700 hover:bg-gray-700 hover:text-gray-200 transition-colors"
          >
            Save as Template
          </button>
        </div>

        {showSaveTemplate && (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-3">
            <p className="text-sm font-medium text-gray-300">Save Current Config as Template</p>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Template Name</label>
              <input
                type="text"
                value={templateName}
                onChange={(e) => setTemplateName(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                placeholder="my-template"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Description (optional)</label>
              <input
                type="text"
                value={templateDesc}
                onChange={(e) => setTemplateDesc(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                placeholder="What this template does"
              />
            </div>
            {templateSaved && (
              <p className="text-xs text-green-400">{templateSaved}</p>
            )}
            <button
              type="button"
              disabled={savingTemplate || !templateName.trim()}
              onClick={async () => {
                setSavingTemplate(true);
                setTemplateSaved("");
                try {
                  const overrides: Record<string, unknown> = {};
                  if (maxIter) overrides.max_iterations = maxIter;
                  if (maxCost) overrides.max_cost_usd = maxCost;
                  if (maxWallTime) overrides.max_wall_time_minutes = maxWallTime;
                  if (interactive) overrides.interactive = interactive;
                  const arc = cleanRole(architect);
                  const imp = cleanRole(implementer);
                  const rev = cleanRole(reviewer);
                  if (arc) overrides.architect = arc;
                  if (imp) overrides.implementer = imp;
                  if (rev) overrides.reviewer = rev;

                  await createTemplate({
                    name: templateName.trim(),
                    description: templateDesc.trim(),
                    request_pattern: request,
                    preset: preset || undefined,
                    overrides,
                    constraints: buildConstraints(),
                    variables: {},
                  });
                  setTemplateSaved(`Template "${templateName.trim()}" saved!`);
                  setTemplateName("");
                  setTemplateDesc("");
                } catch (e) {
                  setError(e instanceof Error ? e.message : "Failed to save template");
                } finally {
                  setSavingTemplate(false);
                }
              }}
              className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-md hover:bg-green-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {savingTemplate ? "Saving..." : "Save Template"}
            </button>
          </div>
        )}
      </form>

      {/* Planning Progress */}
      {planning && (
        <div className="mt-8 bg-gray-900 border border-blue-800 rounded-lg p-6" id="plan-progress">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-5 h-5 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
            <h2 className="text-lg font-semibold text-white">Generating Plan...</h2>
          </div>
          <PlanningProgress />
        </div>
      )}

      {/* Plan Result */}
      {planResult && (
        <div id="plan-result" className="mt-8 bg-gray-900 border border-green-800 rounded-lg p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">Plan Result</h2>
            <div className="flex items-center gap-3">
              <span className="text-xs text-gray-500">
                Cost: ${planResult.cost_usd.toFixed(4)} USD
              </span>
              <button
                type="button"
                onClick={() => setPlanResult(null)}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Dismiss
              </button>
            </div>
          </div>

          {planResult.plan_summary && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-1">Summary</p>
              <p className="text-sm text-gray-200">{planResult.plan_summary}</p>
            </div>
          )}

          {planResult.tasks.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-2">
                Tasks ({planResult.tasks.length})
              </p>
              <div className="space-y-2">
                {planResult.tasks.map((task, i) => (
                  <div key={i} className="bg-gray-800 rounded-md px-4 py-3 border border-gray-700">
                    <p className="text-sm text-white font-medium">
                      {(task as Record<string, unknown>).title as string || `Task ${i + 1}`}
                    </p>
                    {String((task as Record<string, unknown>).description || "") && (
                      <p className="text-xs text-gray-400 mt-1">
                        {String((task as Record<string, unknown>).description)}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {planResult.assumptions.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-2">Assumptions</p>
              <ul className="list-disc list-inside space-y-1">
                {planResult.assumptions.map((a, i) => (
                  <li key={i} className="text-sm text-gray-300">{a}</li>
                ))}
              </ul>
            </div>
          )}

          <button
            type="button"
            onClick={handleExecutePlan}
            disabled={submitting}
            className="w-full py-3 bg-green-600 text-white font-medium rounded-lg hover:bg-green-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? "Starting pipeline..." : "Execute This Plan"}
          </button>
        </div>
      )}
    </div>
  );
}

export default function NewRunPage() {
  return (
    <Suspense>
      <NewRunPageInner />
    </Suspense>
  );
}
