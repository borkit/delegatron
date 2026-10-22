"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { listTemplates, deleteTemplate } from "@/lib/api";
import type { Template } from "@/lib/api";
import ConfirmDialog from "@/components/ConfirmDialog";

export default function TemplatesPage() {
  const router = useRouter();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "builtin" | "user">("all");

  useEffect(() => {
    loadTemplates();
  }, []);

  async function loadTemplates() {
    try {
      const data = await listTemplates();
      setTemplates(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load templates");
    } finally {
      setLoading(false);
    }
  }

  function handleUseTemplate(template: Template) {
    // Navigate to New Run with template data pre-loaded via query params
    const params = new URLSearchParams();
    params.set("template", template.name);
    if (template.request_pattern) {
      // Render placeholders with default variables
      let rendered = template.request_pattern;
      if (template.variables) {
        for (const [key, val] of Object.entries(template.variables)) {
          rendered = rendered.replaceAll(`{${key}}`, val).replaceAll(`\${${key}}`, val);
        }
      }
      params.set("request", rendered);
    }
    if (template.preset) params.set("preset", template.preset);
    if (template.description) params.set("description", template.description);
    // Pass docker image from pipeline overrides
    const pipelineOvr = template.overrides?.pipeline as Record<string, unknown> | undefined;
    if (pipelineOvr?.docker_image) params.set("docker_image", pipelineOvr.docker_image as string);
    router.push(`/new/?${params.toString()}`);
  }

  async function handleDelete() {
    if (!deleteTarget) return;
    try {
      await deleteTemplate(deleteTarget);
      setTemplates((prev) => prev.filter((t) => t.name !== deleteTarget));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete template");
    } finally {
      setDeleteTarget(null);
    }
  }

  if (loading) return <p className="text-gray-500">Loading templates...</p>;

  const q = query.trim().toLowerCase();
  const filtered = templates.filter((t) => {
    if (filter === "builtin" && !t.is_builtin) return false;
    if (filter === "user" && t.is_builtin) return false;
    if (!q) return true;
    return (
      t.name.toLowerCase().includes(q) ||
      (t.description ?? "").toLowerCase().includes(q) ||
      (t.request_pattern ?? "").toLowerCase().includes(q)
    );
  });
  const counts = {
    all: templates.length,
    builtin: templates.filter((t) => t.is_builtin).length,
    user: templates.filter((t) => !t.is_builtin).length,
  };

  return (
    <div className="max-w-3xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Templates</h1>
          <p className="text-sm text-gray-500 mt-1">
            Reusable pipeline configurations. Built-ins ship with Delegatron; save your own from the New Run page.
          </p>
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-400 text-sm mb-6">
          {error}
        </div>
      )}

      {/* Search + filter controls */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`Search ${counts.all} templates by name, description, or pattern…`}
          className="flex-1 min-w-[200px] bg-gray-900 border border-gray-800 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-600"
        />
        <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1">
          {(["all", "builtin", "user"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${
                filter === k
                  ? "bg-blue-600/30 text-blue-300"
                  : "text-gray-400 hover:text-gray-200"
              }`}
              title={`Show ${k === "all" ? "all" : k} templates`}
            >
              {k === "all" ? "All" : k === "builtin" ? "Built-in" : "Mine"} ({counts[k]})
            </button>
          ))}
        </div>
      </div>

      {templates.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-500 mb-2">No templates found.</p>
          <p className="text-xs text-gray-600">
            The built-ins should appear automatically — if you see this, the API probably isn&apos;t reachable.
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-500 mb-2">No templates match &quot;{query}&quot;.</p>
          <button
            onClick={() => setQuery("")}
            className="text-xs text-blue-400 hover:text-blue-300 underline"
          >
            Clear search
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((t) => {
            const varCount = Object.keys(t.variables ?? {}).length;
            return (
              <div
                key={t.name}
                className="bg-gray-900 border border-gray-800 rounded-lg p-4"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h3 className="text-sm font-medium text-white truncate">
                        {t.name}
                      </h3>
                      {t.is_builtin && (
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 border border-gray-700"
                          title="Shipped with Delegatron"
                        >
                          BUILT-IN
                        </span>
                      )}
                      {t.preset && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/50 text-blue-400 border border-blue-800">
                          {t.preset}
                        </span>
                      )}
                      {varCount > 0 && (
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded bg-purple-900/30 text-purple-300 border border-purple-900/50"
                          title={`This template accepts ${varCount} variable(s): ${Object.keys(t.variables ?? {}).join(", ")}`}
                        >
                          {varCount} var{varCount === 1 ? "" : "s"}
                        </span>
                      )}
                    </div>
                    {t.description && (
                      <p className="text-xs text-gray-500 mt-1">{t.description}</p>
                    )}
                    {t.request_pattern && (
                      <p className="text-xs text-gray-600 mt-1 font-mono truncate">
                        {t.request_pattern}
                      </p>
                    )}
                    {varCount > 0 && (
                      <p className="text-[10px] text-purple-300/70 mt-1 font-mono">
                        Variables: {Object.keys(t.variables ?? {}).join(", ")}
                      </p>
                    )}
                  </div>
                  <div className="flex gap-2 ml-4 shrink-0">
                    <button
                      onClick={() => handleUseTemplate(t)}
                      className="px-3 py-1.5 text-xs font-medium bg-blue-600/20 text-blue-400 border border-blue-800 rounded-lg hover:bg-blue-600/30 transition-colors"
                      title="Start a new run using this template's configuration"
                    >
                      Use
                    </button>
                    {!t.is_builtin && (
                      <button
                        onClick={() => setDeleteTarget(t.name)}
                        className="px-3 py-1.5 text-xs font-medium bg-red-600/20 text-red-400 border border-red-800 rounded-lg hover:bg-red-600/30 transition-colors"
                        title="Permanently delete this template"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete template"
        message={`Are you sure you want to delete "${deleteTarget}"? This cannot be undone.`}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
