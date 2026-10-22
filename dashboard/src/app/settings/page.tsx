"use client";

import { useEffect, useRef, useState } from "react";
import { getStoredApiUrl, setApiUrl, getHealth, getProviderKeyStatus, hasApiKey, clearApiKey, setLogLevel as apiSetLogLevel, setLogTimezone as apiSetLogTimezone, getServerDefaults, setParallelTasks as apiSetParallelTasks, setProjectMode as apiSetProjectMode, type ServerDefaults } from "@/lib/api";
import {
  FALLBACK_DEFAULTS,
  migrateImportedSettings,
  migrateLegacyDefaults,
  resetPipelineDefaults,
  resolveFormDefaults,
  savePipelineDefaults,
  type PipelineDefaults,
  type PipelineField,
} from "@/lib/pipelineDefaults";

const PRESETS = [
  { value: "local-oss", label: "Local OSS ★", description: "phi4 + gpt-oss:20b + phi4 — best local quality (validated 2026-04-18: only local model to pass fib + React in both roles). Fits in 12 GB VRAM.", local: true },
  { value: "local", label: "Local (default)", description: "phi4 + qwen2.5-coder:14b + phi4 — 12 GB VRAM baseline, passes fib + React cleanly.", local: true },
  { value: "local-oss-review", label: "Local OSS Review", description: "phi4 + qwen2.5-coder:14b + gpt-oss:20b — strongest local review (~2× reviewer latency for tougher architectural critique).", local: true },
  { value: "local-phi4", label: "Local Phi-4", description: "phi4:14b all roles — fastest local option for simple tasks.", local: true },
  { value: "local-heavy", label: "Local Heavy ⚠️", description: "phi4 + qwen3-coder:30b + phi4 — REQUIRES 24 GB+ VRAM. Spills 30+ GB to RAM on smaller cards, 3× slower.", local: true },
  { value: "default", label: "Cloud Trio", description: "Balanced quality and cost (Claude + GPT + Gemini)", local: false },
  { value: "claude-heavy", label: "Claude Heavy", description: "Claude architect + implementer, Gemini reviewer", local: false },
  { value: "budget", label: "Budget", description: "All Gemini Flash (cheapest cloud option)", local: false },
  { value: "single-claude", label: "Single Claude", description: "All Claude Sonnet (baseline)", local: false },
  { value: "single-gpt", label: "Single GPT", description: "All GPT-4.1 (baseline)", local: false },
  { value: "mock", label: "Mock", description: "Mock providers for testing (no API keys needed)", local: false },
];

function Tip({ text }: { text: string }) {
  return (
    <span className="relative group ml-1 cursor-help">
      <span className="text-gray-600 text-[10px]">[?]</span>
      <span className="absolute left-0 bottom-full mb-1 hidden group-hover:block w-64 p-2 text-xs text-gray-300 bg-gray-800 border border-gray-700 rounded-lg shadow-lg z-50 whitespace-normal">
        {text}
      </span>
    </span>
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

export default function SettingsPage() {
  const [apiUrl, setApiUrlState] = useState("");
  const [selectedPreset, setSelectedPreset] = useState("local");
  const [connectionStatus, setConnectionStatus] = useState<"idle" | "testing" | "ok" | "error">("idle");
  const [connectionMessage, setConnectionMessage] = useState("");

  // Pipeline defaults: stored value > server default > FALLBACK_DEFAULTS
  const [maxIterations, setMaxIterations] = useState(FALLBACK_DEFAULTS.max_iterations);
  const [maxCostUsd, setMaxCostUsd] = useState(FALLBACK_DEFAULTS.max_cost_usd);
  const [maxWallTime, setMaxWallTime] = useState(FALLBACK_DEFAULTS.max_wall_time);
  const [defaultSandbox, setDefaultSandbox] = useState(FALLBACK_DEFAULTS.sandbox);
  const [dockerImage, setDockerImage] = useState("");
  const [defaultWorkspace, setDefaultWorkspace] = useState(FALLBACK_DEFAULTS.workspace);
  // Pipeline fields the user changed since the page loaded or was saved: only
  // these are saved, so untouched fields keep following the server defaults.
  const dirtyRef = useRef<Set<PipelineField>>(new Set());
  const [alertWebhook, setAlertWebhook] = useState("");
  const [pipelineSaved, setPipelineSaved] = useState(false);
  const [pipelineReset, setPipelineReset] = useState(false);
  const [serverDefaults, setServerDefaults] = useState<ServerDefaults | null>(null);
  const [logLevel, setLogLevel] = useState("INFO");
  const [logTimezone, setLogTimezone] = useState<"utc" | "local">("utc");

  // Feature flags
  const [parallelTasks, setParallelTasks] = useState(false);
  const [projectMode, setProjectMode] = useState(false);

  // Provider key status
  const [providerKeys, setProviderKeys] = useState<Record<string, boolean>>({});
  const [keysLoading, setKeysLoading] = useState(false);

  // Auth
  const [hasKey, setHasKey] = useState(false);

  // Show resolved pipeline defaults, leaving the fields the user is editing alone.
  function applyPipelineDefaults(values: PipelineDefaults) {
    const dirty = dirtyRef.current;
    if (!dirty.has("max_iterations")) setMaxIterations(values.max_iterations);
    if (!dirty.has("max_cost_usd")) setMaxCostUsd(values.max_cost_usd);
    if (!dirty.has("max_wall_time")) setMaxWallTime(values.max_wall_time);
    if (!dirty.has("sandbox")) setDefaultSandbox(values.sandbox);
    if (!dirty.has("workspace")) setDefaultWorkspace(values.workspace);
  }

  function markDirty(field: PipelineField) {
    dirtyRef.current.add(field);
  }

  useEffect(() => {
    setApiUrlState(getStoredApiUrl());
    const stored = localStorage.getItem("delegatron_preset");
    if (stored) setSelectedPreset(stored);

    migrateLegacyDefaults(localStorage);
    applyPipelineDefaults(resolveFormDefaults(localStorage, null));
    const storedImage = localStorage.getItem("delegatron_docker_image");
    if (storedImage) setDockerImage(storedImage);
    const storedWebhook = localStorage.getItem("delegatron_alert_webhook");
    if (storedWebhook) setAlertWebhook(storedWebhook);

    setHasKey(hasApiKey());
    loadProviderKeys();

    // Server defaults fill in every pipeline field this browser has not overridden
    getServerDefaults().then((defaults) => {
      setServerDefaults(defaults);
      setLogLevel(defaults.log_level || "INFO");
      if (defaults.log_timezone === "local" || defaults.log_timezone === "utc") setLogTimezone(defaults.log_timezone);
      applyPipelineDefaults(resolveFormDefaults(localStorage, defaults));
      if (defaults.parallel_tasks !== undefined) setParallelTasks(defaults.parallel_tasks);
      if (defaults.project_mode !== undefined) setProjectMode(defaults.project_mode);
    }).catch(() => {});
  }, []);

  async function handleTestConnection() {
    setConnectionStatus("testing");
    try {
      const health = await getHealth();
      setConnectionStatus("ok");
      setConnectionMessage(`Connected - Delegatron v${health.version}`);
    } catch (e) {
      setConnectionStatus("error");
      setConnectionMessage(e instanceof Error ? e.message : "Connection failed");
    }
  }

  function handleSaveApiUrl() {
    setApiUrl(apiUrl);
    setConnectionStatus("idle");
    setConnectionMessage("");
  }

  async function loadProviderKeys() {
    setKeysLoading(true);
    try { setProviderKeys(await getProviderKeyStatus()); } catch { /* */ } finally { setKeysLoading(false); }
  }

  function handlePresetChange(preset: string) {
    setSelectedPreset(preset);
    localStorage.setItem("delegatron_preset", preset);
  }

  async function handleSaveAll() {
    // Save only the pipeline fields the user changed; one equal to the server
    // default is removed instead, so it keeps following the server.
    const values: PipelineDefaults = {
      max_iterations: maxIterations,
      max_cost_usd: maxCostUsd,
      max_wall_time: maxWallTime,
      sandbox: defaultSandbox,
      workspace: defaultWorkspace,
    };
    savePipelineDefaults(localStorage, values, dirtyRef.current, serverDefaults);
    dirtyRef.current = new Set();
    localStorage.setItem("delegatron_docker_image", dockerImage.startsWith("custom:") ? dockerImage.slice(7) : dockerImage);
    localStorage.setItem("delegatron_alert_webhook", alertWebhook);
    // Apply log settings to server
    try { await apiSetLogLevel(logLevel); } catch { /* */ }
    try { await apiSetLogTimezone(logTimezone); } catch { /* */ }
    setPipelineSaved(true);
    setTimeout(() => setPipelineSaved(false), 2000);
  }

  // Forget this browser's pipeline overrides and show the server's defaults again.
  function handleResetPipelineDefaults() {
    resetPipelineDefaults(localStorage);
    dirtyRef.current = new Set();
    applyPipelineDefaults(resolveFormDefaults(localStorage, serverDefaults));
    getServerDefaults().then((defaults) => {
      setServerDefaults(defaults);
      applyPipelineDefaults(resolveFormDefaults(localStorage, defaults));
    }).catch(() => {});
    setPipelineReset(true);
    setTimeout(() => setPipelineReset(false), 2000);
  }

  function handleExport() {
    const settings: Record<string, string | null> = {};
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith("delegatron_")) {
        settings[key] = localStorage.getItem(key);
      }
    }
    const blob = new Blob([JSON.stringify(settings, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "delegatron-settings.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  function handleImport() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json";
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      const text = await file.text();
      const parsed: unknown = JSON.parse(text);
      const settings: Record<string, string> = {};
      if (parsed && typeof parsed === "object") {
        for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
          if (key.startsWith("delegatron_") && typeof value === "string") settings[key] = value;
        }
      }
      // An export from before the settings version would pin the legacy wall time.
      for (const [key, value] of Object.entries(migrateImportedSettings(settings))) {
        localStorage.setItem(key, value);
      }
      window.location.reload();
    };
    input.click();
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-white mb-8">Settings</h1>

      {/* API Endpoint */}
      <section className="mb-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          API Endpoint <Tip text="The URL of your Delegatron server. Default is localhost:8000. Change this if your server runs on a different host or port." />
        </h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <label className="block text-sm text-gray-400 mb-2">
            Delegatron API URL
          </label>
          <div className="flex gap-2">
            <input type="text" value={apiUrl} onChange={(e) => setApiUrlState(e.target.value)}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
              placeholder="http://localhost:8000" />
            <button onClick={handleSaveApiUrl}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-500 transition-colors">
              Save
            </button>
          </div>
          <div className="mt-3">
            <button onClick={handleTestConnection} disabled={connectionStatus === "testing"}
              className="text-sm text-gray-400 hover:text-gray-200 transition-colors disabled:opacity-50">
              {connectionStatus === "testing" ? "Testing..." : "Test connection"}
            </button>
            {connectionMessage && (
              <p className={`text-xs mt-1 ${connectionStatus === "ok" ? "text-green-400" : "text-red-400"}`}>
                {connectionMessage}
              </p>
            )}
          </div>
        </div>
      </section>

      {/* Default Preset */}
      <section>
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          Default Preset <Tip text="The model configuration used when creating new runs. Local presets (green) use Ollama: no API fees, only an estimated electricity and GPU-wear cost. Cloud presets require API keys." />
        </h2>
        <div className="space-y-2">
          {PRESETS.map((preset) => (
            <button key={preset.value} onClick={() => handlePresetChange(preset.value)}
              className={`w-full text-left bg-gray-900 border rounded-lg p-4 transition-colors ${
                selectedPreset === preset.value ? "border-blue-600 bg-blue-600/10" : "border-gray-800 hover:border-gray-700"
              }`}>
              <div className="flex items-center justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium text-white">{preset.label}</p>
                    {preset.local && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/50 text-green-400 border border-green-800">LOCAL</span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">{preset.description}</p>
                </div>
                {selectedPreset === preset.value && <span className="text-blue-400 text-sm">Active</span>}
              </div>
            </button>
          ))}
        </div>
      </section>

      {/* Pipeline Defaults */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          Pipeline Defaults <Tip text="Default values for new pipeline runs. Override per-run in the New Run form. Saved in your browser." />
        </h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Max Iterations <Tip text="Maximum number of implement-test-review cycles. Higher = more retries to get it right. Each iteration uses tokens/cost. The default comes from the server (5 unless DELEGATRON_MAX_ITERATIONS is set)." />
              </label>
              <input type="number" value={maxIterations} onChange={(e) => { markDirty("max_iterations"); setMaxIterations(Math.max(1, Math.min(100, parseInt(e.target.value) || 1))); }}
                min={1} max={100} className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Max Cost (USD) <Tip text="Budget cap per run: the pipeline stops when the run's cost exceeds it. Local (Ollama) runs are not free here: their estimated electricity and GPU wear count toward any non-zero cap. Set 0 for unlimited. The default comes from the server ($5 unless DELEGATRON_MAX_COST_USD is set)." />
              </label>
              <input type="number" value={maxCostUsd} onChange={(e) => { markDirty("max_cost_usd"); setMaxCostUsd(Math.max(0, Math.min(1000, parseFloat(e.target.value) || 0))); }}
                min={0} max={1000} step={0.5} className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Max Wall Time (min) <Tip text="Maximum real-world time in minutes before the pipeline is forcefully stopped. Prevents runaway runs. The default comes from the server (60 minutes unless DELEGATRON_MAX_WALL_TIME is set)." />
              </label>
              <input type="number" value={maxWallTime} onChange={(e) => { markDirty("max_wall_time"); setMaxWallTime(Math.max(1, Math.min(1440, parseInt(e.target.value) || 1))); }}
                min={1} max={1440} className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500" />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Default Sandbox <Tip text="Docker: runs code in an isolated local container with network disabled during tests. E2B: cloud-hosted microVMs (requires E2B API key)." />
              </label>
              <select value={defaultSandbox} onChange={(e) => { markDirty("sandbox"); setDefaultSandbox(e.target.value); }}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
                <option value="docker">Docker</option>
                <option value="e2b">E2B</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Docker Image <Tip text="Container image for the sandbox. Auto-detect uses python:3.12-slim. Pick a preset for other languages or enter a custom image." />
              </label>
              {(dockerImage.startsWith("custom:") || (dockerImage !== "" && !DOCKER_IMAGES.some((i) => i.value === dockerImage))) ? (
                <div className="flex gap-2">
                  <input type="text" value={dockerImage.startsWith("custom:") ? dockerImage.slice(7) : dockerImage}
                    onChange={(e) => setDockerImage(e.target.value || "custom:")}
                    className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500"
                    placeholder="myregistry/myimage:tag" autoFocus />
                  <button type="button" onClick={() => setDockerImage("")}
                    className="px-3 py-2 text-xs bg-gray-700 text-gray-300 rounded-md hover:bg-gray-600">
                    Presets
                  </button>
                </div>
              ) : (
                <select value={dockerImage}
                  onChange={(e) => {
                    if (e.target.value === "__custom__") {
                      setDockerImage("custom:");
                    } else {
                      setDockerImage(e.target.value);
                    }
                  }}
                  className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
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
              )}
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Default Workspace <Tip text="Default path or git URL used when no workspace is specified. Leave blank to use the server's working directory." />
              </label>
              <input type="text" value={defaultWorkspace} onChange={(e) => { markDirty("workspace"); setDefaultWorkspace(e.target.value); }}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500"
                placeholder="server working directory" />
            </div>
          </div>

          {/* Logging */}
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mt-6 mb-3">
            Logging <Tip text="Server logs are saved to the logs/ directory with automatic rotation. Changes apply immediately without restart." />
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Log Level <Tip text="DEBUG: every detail including LLM prompts. INFO: normal operation events. WARNING: issues that don't stop execution. ERROR: only failures." />
              </label>
              <div className="flex gap-2">
                <select value={logLevel} onChange={(e) => setLogLevel(e.target.value)}
                  className="flex-1 bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
                  <option value="DEBUG">DEBUG</option>
                  <option value="INFO">INFO</option>
                  <option value="WARNING">WARNING</option>
                  <option value="ERROR">ERROR</option>
                </select>
                <button onClick={async () => {
                    try { await apiSetLogLevel(logLevel); } catch { /* */ }
                  }} className="px-3 py-2 text-xs bg-gray-700 text-gray-300 rounded-md hover:bg-gray-600">
                  Apply
                </button>
              </div>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Timestamps <Tip text="UTC: consistent across timezones, standard for servers. Local: uses your machine's timezone, easier to read when debugging locally." />
              </label>
              <div className="flex gap-2">
                {(["utc", "local"] as const).map((tz) => (
                  <button key={tz} onClick={async () => {
                      try { await apiSetLogTimezone(tz); setLogTimezone(tz); } catch { /* */ }
                    }} className={`flex-1 px-3 py-2 text-xs rounded-md border transition-colors ${
                      logTimezone === tz
                        ? "bg-blue-600/20 text-blue-400 border-blue-800"
                        : "bg-gray-800 text-gray-400 border-gray-700 hover:bg-gray-700"
                    }`}>
                    {tz === "utc" ? "UTC" : "Local Time"}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Ollama Management */}
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mt-6 mb-3">
            Ollama <Tip text="Restart Ollama if you see repeated failures, stuck runs, or template-collapse behavior (e.g. unrelated plans for every task). Restarting clears the KV cache and unloads stuck models." />
          </h3>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 mb-6">
            <p className="text-xs text-gray-400 mb-3">
              If local models repeatedly produce bad/stuck output, restart Ollama to clear stuck state.
            </p>
            <button
              onClick={async () => {
                if (!confirm("Restart Ollama? This will unload all models and interrupt any running local pipelines.")) return;
                try {
                  const { restartOllama } = await import("@/lib/api");
                  const r = await restartOllama();
                  alert(`Ollama restarted. ${r.models_available} models available.`);
                } catch (e) {
                  alert(`Restart failed: ${e instanceof Error ? e.message : String(e)}`);
                }
              }}
              className="px-4 py-2 bg-orange-700 hover:bg-orange-600 text-white text-sm font-medium rounded-lg transition-colors"
              title="Forcibly restart the Ollama server — clears model cache and stuck state"
            >
              Restart Ollama
            </button>
          </div>

          {/* Experimental Features */}
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mt-6 mb-3">
            Experimental Features <Tip text="These features are opt-in. Enable them to unlock advanced pipeline capabilities." />
          </h3>
          <div className="space-y-3">
            <label className="flex items-start gap-3 p-3 rounded-lg border border-gray-800 bg-gray-900 cursor-pointer hover:border-gray-700 transition-colors" title="When the architect produces multiple independent tasks, run them in parallel using git worktrees. Each task gets its own isolated copy of the repo. Best with single-model presets on local GPU.">
              <input
                type="checkbox"
                checked={parallelTasks}
                onChange={async (e) => {
                  const enabled = e.target.checked;
                  setParallelTasks(enabled);
                  try { await apiSetParallelTasks(enabled); } catch { /* */ }
                }}
                className="mt-0.5 w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-600"
              />
              <div>
                <span className="text-sm font-medium text-white">Parallel Task Execution</span>
                <p className="text-xs text-gray-500 mt-0.5">Run independent tasks concurrently using git worktrees. Speeds up complex jobs where the architect creates multiple independent subtasks. Works best when using a single implementer model to avoid GPU model swapping overhead.</p>
              </div>
            </label>

            <label className="flex items-start gap-3 p-3 rounded-lg border border-gray-800 bg-gray-900 cursor-pointer hover:border-gray-700 transition-colors" title="Decompose large projects into milestones and run each through the full pipeline sequentially. The workspace accumulates code across milestones.">
              <input
                type="checkbox"
                checked={projectMode}
                onChange={async (e) => {
                  const enabled = e.target.checked;
                  setProjectMode(enabled);
                  try { await apiSetProjectMode(enabled); } catch { /* */ }
                }}
                className="mt-0.5 w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-600"
              />
              <div>
                <span className="text-sm font-medium text-white">Project Mode</span>
                <p className="text-xs text-gray-500 mt-0.5">Build complete projects from high-level descriptions. A meta-architect breaks the project into milestones, each executed through the full pipeline. Milestones are sequential — each builds on previous code. Enables the &ldquo;New Project&rdquo; page and <code>delegatron project</code> CLI command.</p>
              </div>
            </label>
          </div>

          {/* Alerting */}
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mt-6 mb-3">
            Alerting <Tip text="Get notified when pipeline runs fail, are cancelled, or exhaust their budget. Sends a POST request with run details." />
          </h3>
          <div>
            <label className="block text-xs text-gray-400 mb-1">
              Alert Webhook URL <Tip text="URL that receives a POST request with JSON payload when a run fails. Works with Slack incoming webhooks, Discord webhooks, PagerDuty, or any HTTP endpoint. Leave blank to disable." />
            </label>
            <input type="text" value={alertWebhook} onChange={(e) => setAlertWebhook(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500"
              placeholder="https://hooks.slack.com/services/..." />
          </div>

          <div className="mt-5 flex items-center gap-3">
            <button onClick={handleSaveAll}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-500 transition-colors">
              Save All Settings
            </button>
            <button onClick={handleResetPipelineDefaults}
              className="px-4 py-2 bg-gray-800 text-gray-300 text-sm rounded-md hover:bg-gray-700 transition-colors"
              title="Forget the max iterations, cost, wall time, sandbox and workspace saved in this browser and use the server's defaults">
              Reset pipeline defaults
            </button>
            {pipelineSaved && <span className="text-xs text-green-400">Saved!</span>}
            {pipelineReset && <span className="text-xs text-green-400">Server defaults restored</span>}
          </div>
        </div>
      </section>

      {/* API Key Status */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          API Key Status <Tip text="Shows which LLM provider API keys are configured on the server via environment variables. Local Ollama presets don't need any keys." />
        </h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          {keysLoading ? (
            <p className="text-xs text-gray-500">Loading...</p>
          ) : Object.keys(providerKeys).length === 0 ? (
            <div>
              <p className="text-xs text-gray-600 mb-2">Could not connect to server to check key status.</p>
              <button onClick={loadProviderKeys} className="text-xs text-gray-400 hover:text-gray-200 transition-colors">Retry</button>
            </div>
          ) : (
            <div className="space-y-2">
              {Object.entries(providerKeys).map(([key, isSet]) => (
                <div key={key} className="flex items-center justify-between py-1.5">
                  <span className="text-sm text-gray-300 font-mono">{key}</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${
                    isSet ? "bg-green-900/30 text-green-400 border border-green-800" : "bg-gray-800 text-gray-600 border border-gray-700"
                  }`}>
                    {isSet ? "Configured" : "Not set"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Authentication */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          Authentication <Tip text="When DELEGATRON_AUTH_ENABLED=true on the server, all API requests require a Bearer token. Store your API key here for the dashboard to use." />
        </h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-300">Dashboard API Key</p>
              <p className="text-xs text-gray-500 mt-0.5">
                {hasKey ? "An API key is stored in your browser." : "No API key stored. Set one if auth is enabled on the server."}
              </p>
            </div>
            {hasKey && (
              <button onClick={() => { clearApiKey(); setHasKey(false); }}
                className="px-3 py-1.5 text-xs font-medium bg-red-600/20 text-red-400 border border-red-800 rounded-lg hover:bg-red-600/30 transition-colors">
                Clear Key
              </button>
            )}
          </div>
        </div>
      </section>

      {/* Export/Import */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
          Backup <Tip text="Export all dashboard settings to a JSON file, or import settings from a previous export. Useful for sharing configuration across machines." />
        </h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 flex gap-3">
          <button onClick={handleExport}
            className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors">
            Export Settings
          </button>
          <button onClick={handleImport}
            className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors">
            Import Settings
          </button>
        </div>
      </section>

      {/* About */}
      <section className="mt-8 mb-8">
        <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">About</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-xs text-gray-500 space-y-2">
          <p className="text-sm text-gray-300 font-medium">
            Delegatron — Multi-agent code bootstrap pipeline
          </p>
          <p>
            Generates new code from scratch: REST APIs, CLI tools, microservices,
            test suites. Not a bug-fixer for existing codebases.
          </p>
          <p className="pt-2 border-t border-gray-800">
            StoriaBrevi LLC · AGPL-3.0 · No support offered · Use at your own risk
          </p>
          <p className="font-mono">Dashboard connected to: {getStoredApiUrl()}</p>
        </div>
      </section>
    </div>
  );
}
