"use client";

import { useState, useEffect } from "react";
import { listRuns, getRun, type RunSummary } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";

export default function ComparePage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [left, setLeft] = useState<RunSummary | null>(null);
  const [right, setRight] = useState<RunSummary | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    listRuns()
      .then((data) => setRuns(data.filter((r) => r.status !== "running" && r.status !== "started")))
      .catch(() => {});
  }, []);

  async function loadComparison() {
    if (!leftId || !rightId) return;
    setLoading(true);
    try {
      const [l, r] = await Promise.all([getRun(leftId), getRun(rightId)]);
      setLeft(l);
      setRight(r);
    } catch {
      setLeft(null);
      setRight(null);
    }
    setLoading(false);
  }

  useEffect(() => {
    if (leftId && rightId) loadComparison();
  }, [leftId, rightId]);

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-bold text-white mb-2">Compare Runs</h1>
      <p className="text-sm text-gray-500 mb-6">
        Side-by-side comparison of two pipeline runs.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-8">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Run A</label>
          <select
            value={leftId}
            onChange={(e) => setLeftId(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          >
            <option value="">Select run...</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id.slice(0, 8)} — {r.status} — {r.request?.slice(0, 40) || "—"}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Run B</label>
          <select
            value={rightId}
            onChange={(e) => setRightId(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          >
            <option value="">Select run...</option>
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id.slice(0, 8)} — {r.status} — {r.request?.slice(0, 40) || "—"}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading && <p className="text-gray-500 text-sm">Loading comparison...</p>}

      {left && right && (
        <div className="space-y-4">
          {/* Header comparison */}
          <div className="grid grid-cols-2 gap-4">
            <RunHeader run={left} label="A" />
            <RunHeader run={right} label="B" />
          </div>

          {/* Metrics comparison table */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800">
                  <th className="px-4 py-2 text-left text-xs text-gray-500 uppercase">Metric</th>
                  <th className="px-4 py-2 text-right text-xs text-gray-500 uppercase">Run A</th>
                  <th className="px-4 py-2 text-right text-xs text-gray-500 uppercase">Run B</th>
                  <th className="px-4 py-2 text-right text-xs text-gray-500 uppercase">Diff</th>
                </tr>
              </thead>
              <tbody>
                <CompareRow
                  label="Status"
                  a={left.status}
                  b={right.status}
                />
                <CompareRow
                  label="Iterations"
                  a={left.iterations?.toString() || "0"}
                  b={right.iterations?.toString() || "0"}
                  diff={numDiff(left.iterations, right.iterations)}
                />
                <CompareRow
                  label="Cost"
                  a={`$${(left.total_cost_usd || 0).toFixed(4)}`}
                  b={`$${(right.total_cost_usd || 0).toFixed(4)}`}
                  diff={costDiff(left.total_cost_usd, right.total_cost_usd)}
                />
                <CompareRow
                  label="Duration"
                  a={`${(left.elapsed_seconds || 0).toFixed(1)}s`}
                  b={`${(right.elapsed_seconds || 0).toFixed(1)}s`}
                  diff={timeDiff(left.elapsed_seconds, right.elapsed_seconds)}
                />
                <CompareRow
                  label="Approved"
                  a={left.is_approved ? "Yes" : "No"}
                  b={right.is_approved ? "Yes" : "No"}
                />
                <CompareRow
                  label="Files"
                  a={((left as any).files_created?.length || 0).toString()}
                  b={((right as any).files_created?.length || 0).toString()}
                />
                <CompareRow
                  label="Tests"
                  a={(left as any).test_report ? `${(left as any).test_report.passed ? "Pass" : "Fail"} (${(left as any).test_report.total})` : "—"}
                  b={(right as any).test_report ? `${(right as any).test_report.passed ? "Pass" : "Fail"} (${(right as any).test_report.total})` : "—"}
                />
              </tbody>
            </table>
          </div>

          {/* Request comparison */}
          {(left.request || right.request) && (
            <div className="grid grid-cols-2 gap-4">
              <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
                <p className="text-xs text-gray-500 uppercase mb-2">Request A</p>
                <p className="text-sm text-gray-300">{left.request || "—"}</p>
              </div>
              <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
                <p className="text-xs text-gray-500 uppercase mb-2">Request B</p>
                <p className="text-sm text-gray-300">{right.request || "—"}</p>
              </div>
            </div>
          )}
        </div>
      )}

      {!left && !right && !loading && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-400 mb-2">Select two runs to compare</p>
          <p className="text-gray-600 text-sm">See how different presets, models, or prompts affect results.</p>
        </div>
      )}
    </div>
  );
}

function RunHeader({ run, label }: { run: RunSummary; label: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-xs font-bold text-gray-500 bg-gray-800 px-2 py-0.5 rounded">{label}</span>
        <code className="text-sm text-gray-300 font-mono">{run.run_id.slice(0, 8)}</code>
        <StatusBadge status={run.status} />
      </div>
      <p className="text-xs text-gray-500 truncate mt-1">{run.request || "—"}</p>
    </div>
  );
}

function CompareRow({ label, a, b, diff }: { label: string; a: string; b: string; diff?: { text: string; color: string } }) {
  return (
    <tr className="border-b border-gray-800/50">
      <td className="px-4 py-2 text-gray-400">{label}</td>
      <td className="px-4 py-2 text-right text-gray-300 font-mono">{a}</td>
      <td className="px-4 py-2 text-right text-gray-300 font-mono">{b}</td>
      <td className={`px-4 py-2 text-right font-mono text-xs ${diff?.color || "text-gray-600"}`}>
        {diff?.text || (a === b ? "=" : "\u2260")}
      </td>
    </tr>
  );
}

function numDiff(a?: number, b?: number): { text: string; color: string } | undefined {
  if (a === undefined || b === undefined) return undefined;
  const d = b - a;
  if (d === 0) return { text: "=", color: "text-gray-600" };
  return { text: d > 0 ? `+${d}` : `${d}`, color: d > 0 ? "text-yellow-400" : "text-green-400" };
}

function costDiff(a?: number, b?: number): { text: string; color: string } | undefined {
  if (!a && !b) return undefined;
  const d = (b || 0) - (a || 0);
  if (Math.abs(d) < 0.0001) return { text: "=", color: "text-gray-600" };
  const pct = a ? ((d / a) * 100).toFixed(0) : "N/A";
  return {
    text: `${d > 0 ? "+" : ""}$${d.toFixed(4)} (${pct}%)`,
    color: d > 0 ? "text-red-400" : "text-green-400",
  };
}

function timeDiff(a?: number, b?: number): { text: string; color: string } | undefined {
  if (!a && !b) return undefined;
  const d = (b || 0) - (a || 0);
  if (Math.abs(d) < 0.1) return { text: "=", color: "text-gray-600" };
  return {
    text: `${d > 0 ? "+" : ""}${d.toFixed(1)}s`,
    color: d > 0 ? "text-yellow-400" : "text-green-400",
  };
}
