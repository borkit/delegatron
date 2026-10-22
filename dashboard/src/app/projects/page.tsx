"use client";

import { useState, useEffect, useRef } from "react";
import {
  createProject,
  getProject,
  listProjects,
  getServerDefaults,
  type ProjectData,
  type ProjectListItem,
} from "@/lib/api";

const PRESETS = [
  { value: "local-oss", label: "Local OSS ★", desc: "phi4 + gpt-oss:20b + phi4 — best local quality (validated 2026-04-18)" },
  { value: "local", label: "Local (default)", desc: "phi4 + qwen2.5-coder:14b (fits 12 GB VRAM)" },
  { value: "local-oss-review", label: "Local OSS Review", desc: "qwen2.5-coder + gpt-oss:20b reviewer (tougher critique)" },
  { value: "local-phi4", label: "Local Phi-4", desc: "phi4:14b all roles (fits 12 GB, fastest)" },
  { value: "local-heavy", label: "Local Heavy ⚠️", desc: "qwen3-coder:30b (requires 24 GB+ VRAM)" },
  { value: "default", label: "Cloud Trio", desc: "Claude + GPT + Gemini" },
  { value: "claude-heavy", label: "Claude Heavy", desc: "Claude architect + implementer" },
  { value: "budget", label: "Budget", desc: "All Gemini Flash" },
  { value: "single-claude", label: "Single Claude", desc: "Claude Sonnet all roles" },
  { value: "single-gpt", label: "Single GPT", desc: "GPT-4.1 all roles" },
];

export default function ProjectsPage() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [request, setRequest] = useState("");
  const [preset, setPreset] = useState("default");
  const [maxMilestones, setMaxMilestones] = useState(10);
  const [maxIterations, setMaxIterations] = useState(5);
  const [maxCost, setMaxCost] = useState(50);
  const [maxWallTime, setMaxWallTime] = useState(120);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [projectId, setProjectId] = useState<string | null>(null);
  const [projectData, setProjectData] = useState<ProjectData | null>(null);
  const [history, setHistory] = useState<ProjectListItem[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Check if project mode is enabled
  useEffect(() => {
    getServerDefaults()
      .then((d) => setEnabled(d.project_mode))
      .catch(() => setEnabled(false));
    listProjects().then(setHistory).catch(() => {});
  }, []);

  // Poll active project
  useEffect(() => {
    if (!projectId) return;
    async function poll() {
      try {
        const data = await getProject(projectId!);
        setProjectData(data);
        if (data.status !== "running") {
          setSubmitting(false);
          if (pollRef.current) clearInterval(pollRef.current);
          listProjects().then(setHistory).catch(() => {});
        }
      } catch {}
    }
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [projectId]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!request.trim()) return;
    setSubmitting(true);
    setError("");
    setProjectData(null);
    try {
      const res = await createProject({
        request: request.trim(),
        preset,
        max_milestones: maxMilestones,
        max_iterations: maxIterations,
        max_cost_usd: maxCost,
        max_wall_time_minutes: maxWallTime,
      });
      setProjectId(res.project_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start project");
      setSubmitting(false);
    }
  }

  if (enabled === null) {
    return <p className="text-gray-500">Loading...</p>;
  }

  if (!enabled) {
    return (
      <div className="max-w-2xl">
        <h1 className="text-2xl font-bold text-white mb-4">Project Mode</h1>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-center">
          <p className="text-gray-400 mb-3">Project mode is currently disabled.</p>
          <p className="text-sm text-gray-500 mb-4">
            Enable it in <a href="/settings" className="text-blue-400 hover:text-blue-300">Settings</a> under
            &ldquo;Experimental Features&rdquo; to build complete projects from high-level descriptions.
          </p>
          <a href="/settings" className="inline-block px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors" title="Open Settings to enable Project Mode under Experimental Features">
            Go to Settings
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-bold text-white mb-2">Project Mode</h1>
      <p className="text-sm text-gray-500 mb-6">
        Build complete projects from a high-level description. The meta-architect breaks it into milestones,
        each executed through the full pipeline. Code accumulates across milestones.
      </p>

      {/* New project form */}
      {!projectData && (
        <form onSubmit={handleSubmit} className="bg-gray-900 border border-gray-800 rounded-lg p-6 space-y-4 mb-6">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Project Description</label>
            <textarea
              value={request}
              onChange={(e) => setRequest(e.target.value)}
              rows={4}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 resize-y"
              placeholder="Describe the complete project you want to build..."
              title="Describe the full project — the meta-architect will decompose it into milestones automatically"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Preset</label>
              <select
                value={preset} onChange={(e) => setPreset(e.target.value)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white"
                title="Which model configuration to use for all milestones — single-model presets avoid GPU swapping overhead on local GPU"
              >
                {PRESETS.map((p) => (
                  <option key={p.value} value={p.value}>{p.label} — {p.desc}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Max Milestones</label>
              <input type="number" value={maxMilestones} min={2} max={20}
                onChange={(e) => setMaxMilestones(parseInt(e.target.value) || 10)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white"
                title="Maximum number of milestones the meta-architect can create — more milestones = finer granularity but longer total time"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Max Iterations (per milestone)</label>
              <input type="number" value={maxIterations} min={1} max={10}
                onChange={(e) => setMaxIterations(parseInt(e.target.value) || 5)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white"
                title="How many implement/test/review cycles each milestone gets before giving up — higher = more chances to fix issues"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Total Cost Cap ($)</label>
              <input type="number" value={maxCost} min={1} max={200} step={1}
                onChange={(e) => setMaxCost(parseFloat(e.target.value) || 50)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white"
                title="Maximum total spend across all milestones — the project stops if this budget is exhausted. $0 for local-only models."
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Wall-Time Cap (minutes)</label>
              <input type="number" value={maxWallTime} min={10} max={480} step={10}
                onChange={(e) => setMaxWallTime(parseInt(e.target.value) || 120)}
                className="w-full bg-gray-800 border border-gray-700 rounded-md px-3 py-2 text-sm text-white"
                title="Maximum total elapsed time across all milestones — prevents runaway jobs"
              />
            </div>
          </div>

          {error && (
            <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-400 text-sm">{error}</div>
          )}

          <button type="submit" disabled={submitting || !request.trim()}
            className="w-full py-3 bg-purple-600 text-white font-medium rounded-lg hover:bg-purple-500 transition-colors disabled:opacity-50"
            title="Decompose the project into milestones and run each through the full pipeline"
          >
            {submitting ? `Building Project (${(projectData as ProjectData | null)?.milestones_completed || 0}/${(projectData as ProjectData | null)?.milestones_total || "?"})...` : "Build Project"}
          </button>
        </form>
      )}

      {/* Active project results */}
      {projectData && (
        <div className="space-y-4 mb-6">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">
              {projectData.plan?.summary || "Building..."}
              <span className="text-xs text-gray-600 font-mono ml-2">{projectId}</span>
            </h2>
            <span className={`text-xs font-medium px-2 py-1 rounded ${
              projectData.status === "completed" ? "bg-green-900/50 text-green-400 border border-green-800" :
              projectData.status === "completed_partial" ? "bg-yellow-900/50 text-yellow-400 border border-yellow-800" :
              projectData.status === "failed" ? "bg-red-900/50 text-red-400 border border-red-800" :
              "bg-blue-900/50 text-blue-400 border border-blue-800"
            }`}>
              {projectData.status === "running"
                ? `Running (${projectData.milestones_completed}/${projectData.milestones_total})`
                : projectData.status}
            </span>
          </div>

          {/* Stats */}
          <div className="grid grid-cols-4 gap-3">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-500">Milestones</p>
              <p className="text-lg font-bold text-white mt-1">{projectData.milestones_completed}/{projectData.milestones_total}</p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-500">Total Cost</p>
              <p className="text-lg font-bold text-white mt-1">${(projectData.total_cost_usd || 0).toFixed(4)}</p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-500">Duration</p>
              <p className="text-lg font-bold text-white mt-1">{((projectData.total_time_seconds || 0) / 60).toFixed(1)}m</p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-500">Preset</p>
              <p className="text-lg font-bold text-white mt-1">{projectData.preset}</p>
            </div>
          </div>

          {/* Milestone timeline */}
          {projectData.plan && (
            <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800">
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">#</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">Milestone</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">Status</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">Iter</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">Cost</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {projectData.plan.milestones.map((m, i) => {
                    const result = projectData.results[i];
                    const isActive = projectData.status === "running" && i === projectData.results.length;
                    return (
                      <tr key={m.id} className={`border-b border-gray-800 last:border-b-0 ${isActive ? "bg-blue-900/10" : ""}`}>
                        <td className="px-4 py-3 text-gray-500 font-mono">{i + 1}</td>
                        <td className="px-4 py-3">
                          <p className="font-medium text-white">{m.title}</p>
                          <p className="text-xs text-gray-500 mt-0.5 line-clamp-1">{m.description}</p>
                        </td>
                        <td className="px-4 py-3">
                          {result ? (
                            <span className={`text-xs font-medium ${
                              result.status === "approved" ? "text-green-400" :
                              result.status === "error" ? "text-red-400" :
                              "text-yellow-400"
                            }`}>
                              {result.status}
                            </span>
                          ) : isActive ? (
                            <span className="text-xs text-blue-400 animate-pulse">Running...</span>
                          ) : (
                            <span className="text-xs text-gray-600">Pending</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-right text-gray-300">
                          {result ? result.iterations : "—"}
                        </td>
                        <td className="px-4 py-3 text-right text-gray-300">
                          {result ? `$${result.cost_usd.toFixed(4)}` : "—"}
                        </td>
                        <td className="px-4 py-3 text-right text-gray-300">
                          {result ? `${result.elapsed_seconds.toFixed(0)}s` : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Error */}
          {projectData.error && (
            <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-400 text-sm">
              {projectData.error}
            </div>
          )}

          {/* New project button after completion */}
          {projectData.status !== "running" && (
            <button
              onClick={() => { setProjectData(null); setProjectId(null); setRequest(""); }}
              className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors"
              title="Clear results and start a new project from scratch"
            >
              Start New Project
            </button>
          )}
        </div>
      )}

      {/* Project history */}
      {history.length > 0 && !projectData && (
        <div>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Past Projects</h2>
          <div className="space-y-2">
            {history.map((h) => (
              <div
                key={h.project_id}
                className="bg-gray-900 border border-gray-800 rounded-lg p-3 cursor-pointer hover:border-gray-700 transition-colors"
                onClick={() => { setProjectId(h.project_id); getProject(h.project_id).then(setProjectData).catch(() => {}); }}
              >
                <div className="flex items-center justify-between">
                  <div className="flex-1 min-w-0 mr-4">
                    <p className="text-sm text-white truncate">{h.request}</p>
                    <div className="flex items-center gap-3 text-xs text-gray-500 mt-1">
                      <span className="font-mono">{h.project_id.slice(0, 16)}</span>
                      <span>{h.preset}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <span className={`text-sm font-bold ${
                      h.status === "completed" ? "text-green-400" :
                      h.status === "failed" ? "text-red-400" :
                      "text-yellow-400"
                    }`}>
                      {h.milestones_completed}/{h.milestones_total}
                    </span>
                    <span className="text-xs text-gray-500">${h.total_cost_usd.toFixed(4)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
