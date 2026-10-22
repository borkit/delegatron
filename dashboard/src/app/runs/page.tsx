"use client";

import { useEffect, useState } from "react";
import type { RunSummary } from "@/lib/api";
import { listRuns, deleteRun, getServerDefaults } from "@/lib/api";
import RunCard from "@/components/RunCard";
import ConfirmDialog from "@/components/ConfirmDialog";

type SortField = "date" | "status" | "cost" | "duration" | "iterations";
type SortDir = "desc" | "asc";

function sortRuns(runs: RunSummary[], field: SortField, dir: SortDir): RunSummary[] {
  const sorted = [...runs].sort((a, b) => {
    let cmp = 0;
    switch (field) {
      case "date":
        cmp = (a.created_at ?? "").localeCompare(b.created_at ?? "");
        break;
      case "status":
        cmp = (a.status ?? "").localeCompare(b.status ?? "");
        break;
      case "cost":
        cmp = (a.total_cost_usd ?? 0) - (b.total_cost_usd ?? 0);
        break;
      case "duration":
        cmp = (a.elapsed_seconds ?? 0) - (b.elapsed_seconds ?? 0);
        break;
      case "iterations":
        cmp = (a.iterations ?? 0) - (b.iterations ?? 0);
        break;
    }
    return dir === "desc" ? -cmp : cmp;
  });
  return sorted;
}

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>("all");
  const [sortField, setSortField] = useState<SortField>("date");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [timezone, setTimezone] = useState<"utc" | "local">("utc");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkAction, setBulkAction] = useState<"delete" | null>(null);
  const [processing, setProcessing] = useState(false);

  async function load() {
    try {
      const [runsData, defaults] = await Promise.all([
        listRuns(),
        getServerDefaults(),
      ]);
      setRuns(runsData);
      if (defaults.log_timezone === "utc" || defaults.log_timezone === "local") {
        setTimezone(defaults.log_timezone);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch runs");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  const filtered = filter === "all" ? runs : runs.filter((r) => r.status === filter);
  const sorted = sortRuns(filtered, sortField, sortDir);
  const statuses = ["all", ...Array.from(new Set(runs.map((r) => r.status)))];
  const allFilteredIds = new Set(sorted.map((r) => r.run_id));
  const allSelected = sorted.length > 0 && sorted.every((r) => selected.has(r.run_id));

  function toggleSelect(runId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }

  function toggleSelectAll() {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(sorted.map((r) => r.run_id)));
    }
  }

  async function handleBulkDelete() {
    setBulkAction(null);
    setProcessing(true);
    const toDelete = Array.from(selected);
    for (const id of toDelete) {
      try {
        await deleteRun(id);
      } catch { /* continue */ }
    }
    setSelected(new Set());
    setProcessing(false);
    await load();
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-white">Run History</h1>
        <a href="/new" className="px-3 py-1.5 text-xs font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors" title="Start a new pipeline run">
          + New Run
        </a>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 mb-4 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Filter tabs + sort + bulk actions */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-4">
          <div className="flex gap-2">
            {statuses.map((s) => (
              <button
                key={s}
                onClick={() => { setFilter(s); setSelected(new Set()); }}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  filter === s
                    ? "bg-blue-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                }`}
              >
                {s}
                {s !== "all" && (
                  <span className="ml-1 text-gray-500">
                    {runs.filter((r) => r.status === s).length}
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Sort controls */}
          <div className="flex items-center gap-1.5 text-xs text-gray-500">
            <span>Sort:</span>
            <select
              value={sortField}
              onChange={(e) => setSortField(e.target.value as SortField)}
              className="px-2 py-1.5 bg-gray-800 text-gray-300 border border-gray-700 rounded-md focus:outline-none focus:border-gray-600"
            >
              <option value="date">Date</option>
              <option value="status">Status</option>
              <option value="cost">Cost</option>
              <option value="duration">Duration</option>
              <option value="iterations">Iterations</option>
            </select>
            <button
              onClick={() => setSortDir((d) => (d === "desc" ? "asc" : "desc"))}
              className="px-2 py-1.5 bg-gray-800 text-gray-400 border border-gray-700 rounded-md hover:bg-gray-700 transition-colors"
              title={sortDir === "desc" ? "Newest first" : "Oldest first"}
            >
              {sortDir === "desc" ? "New \u2192 Old" : "Old \u2192 New"}
            </button>
          </div>
        </div>

        {/* Bulk action bar */}
        {selected.size > 0 && (
          <div className="flex items-center gap-3">
            <span className="text-xs text-gray-400">{selected.size} selected</span>
            <button
              onClick={() => setBulkAction("delete")}
              disabled={processing}
              className="px-3 py-1.5 text-xs font-medium bg-red-600/20 text-red-400 border border-red-800 rounded-lg hover:bg-red-600/30 transition-colors disabled:opacity-50"
            >
              {processing ? "Deleting..." : "Delete selected"}
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-300"
            >
              Clear
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <p className="text-gray-500">Loading runs...</p>
      ) : sorted.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          {runs.length === 0 ? (
            <>
              <p className="text-gray-400 mb-2">No runs yet</p>
              <p className="text-gray-600 text-sm mb-4">Create your first pipeline run to get started.</p>
              <a href="/new" className="inline-block px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors">
                New Run
              </a>
            </>
          ) : (
            <>
              <p className="text-gray-400 mb-1">No runs match &ldquo;{filter}&rdquo;</p>
              <button onClick={() => setFilter("all")} className="text-blue-400 text-sm hover:text-blue-300">
                Show all runs
              </button>
            </>
          )}
        </div>
      ) : (
        <>
          {/* Select all header */}
          <div className="flex items-center gap-3 px-4 py-2 mb-2 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={toggleSelectAll}
              className="w-4 h-4 rounded border-gray-600 bg-gray-800 text-blue-600"
            />
            <span className="cursor-pointer" onClick={toggleSelectAll}>
              {allSelected ? "Deselect all" : "Select all"}
            </span>
            <span className="ml-auto">{sorted.length} run{sorted.length !== 1 ? "s" : ""}</span>
          </div>

          <div className="space-y-2">
            {sorted.map((run) => (
              <div key={run.run_id || `run-${sorted.indexOf(run)}`} className="flex items-start gap-3">
                <input
                  type="checkbox"
                  checked={selected.has(run.run_id)}
                  onChange={() => toggleSelect(run.run_id)}
                  className="w-4 h-4 mt-4 rounded border-gray-600 bg-gray-800 text-blue-600 shrink-0"
                />
                <div className="flex-1 min-w-0">
                  <RunCard run={run} timezone={timezone} />
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <ConfirmDialog
        open={bulkAction === "delete"}
        title="Delete selected runs"
        message={`Delete ${selected.size} run${selected.size !== 1 ? "s" : ""}? This cannot be undone.`}
        confirmLabel={`Delete ${selected.size}`}
        variant="danger"
        onConfirm={handleBulkDelete}
        onCancel={() => setBulkAction(null)}
      />
    </div>
  );
}
