"use client";

import { useEffect, useState } from "react";
import type { UsageSummary } from "@/lib/api";
import { getAnalytics, getLeaderboard } from "@/lib/api";

interface LeaderboardEntry {
  preset: string;
  runs: number;
  approval_rate: number;
  error_rate: number;
  avg_cost: number;
  total_cost: number;
  avg_iterations: number;
}

export default function AnalyticsPage() {
  const [data, setData] = useState<UsageSummary | null>(null);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const [analytics, lb] = await Promise.all([
          getAnalytics(),
          getLeaderboard().catch(() => []) as Promise<LeaderboardEntry[]>,
        ]);
        setData(analytics);
        setLeaderboard(lb);
      } catch (e) {
        setError(
          e instanceof Error ? e.message : "Failed to fetch analytics"
        );
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  if (loading) return <p className="text-gray-500">Loading analytics...</p>;
  if (error) {
    return (
      <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-400 text-sm">
        {error}
      </div>
    );
  }
  if (!data) {
    return (
      <div>
        <h1 className="text-2xl font-bold text-white mb-8">Analytics</h1>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-400 mb-2">No analytics data yet</p>
          <p className="text-gray-600 text-sm">Run some pipelines to see usage stats here.</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-white mb-8">Analytics</h1>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <StatCard label="Total Runs" value={(data.total_runs ?? 0).toString()} />
        <StatCard
          label="Total Spend"
          value={`$${(data.total_cost_usd ?? 0).toFixed(2)}`}
          accent="text-yellow-400"
        />
        <StatCard
          label="Avg Cost/Run"
          value={`$${(data.avg_cost_per_run ?? 0).toFixed(4)}`}
        />
        <StatCard
          label="Success Rate"
          value={`${((data.success_rate ?? 0) * 100).toFixed(0)}%`}
          accent="text-green-400"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
            Runs by Status
          </h2>
          <BarChart data={data.runs_by_status} color="bg-blue-500" />
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
            Cost by Model
          </h2>
          <BarChart
            data={data.cost_by_model}
            color="bg-purple-500"
            formatValue={(v) => `$${v.toFixed(4)}`}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider">
            Avg Iterations
          </p>
          <p className="text-xl font-bold text-white mt-1">
            {(data.avg_iterations ?? 0).toFixed(1)}
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider">
            Most Used Preset
          </p>
          <p className="text-xl font-bold text-white mt-1">
            {data.most_used_preset || "N/A"}
          </p>
        </div>
      </div>

      {/* Preset Performance */}
      {leaderboard.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 mt-8">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-4">
            Preset Performance
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800">
                  <th className="px-3 py-2 text-left text-xs text-gray-500">Preset</th>
                  <th className="px-3 py-2 text-right text-xs text-gray-500">Runs</th>
                  <th className="px-3 py-2 text-right text-xs text-gray-500">Approval</th>
                  <th className="px-3 py-2 text-right text-xs text-gray-500">Errors</th>
                  <th className="px-3 py-2 text-right text-xs text-gray-500">Avg Cost</th>
                  <th className="px-3 py-2 text-right text-xs text-gray-500">Avg Iter</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.map((row) => (
                  <tr key={row.preset} className="border-b border-gray-800/50">
                    <td className="px-3 py-2 text-gray-300 font-mono">{row.preset}</td>
                    <td className="px-3 py-2 text-right text-gray-400">{row.runs}</td>
                    <td className={`px-3 py-2 text-right font-medium ${row.approval_rate >= 0.8 ? "text-green-400" : row.approval_rate >= 0.5 ? "text-yellow-400" : "text-red-400"}`}>
                      {(row.approval_rate * 100).toFixed(0)}%
                    </td>
                    <td className={`px-3 py-2 text-right ${row.error_rate > 0.2 ? "text-red-400" : "text-gray-500"}`}>
                      {(row.error_rate * 100).toFixed(0)}%
                    </td>
                    <td className="px-3 py-2 text-right text-gray-400 font-mono">${row.avg_cost.toFixed(4)}</td>
                    <td className="px-3 py-2 text-right text-gray-400">{row.avg_iterations}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${accent || "text-white"}`}>
        {value}
      </p>
    </div>
  );
}

function BarChart({
  data,
  color,
  formatValue,
}: {
  data: Record<string, number>;
  color: string;
  formatValue?: (v: number) => string;
}) {
  const entries = Object.entries(data ?? {});
  if (entries.length === 0) {
    return <p className="text-gray-500 text-sm">No data available.</p>;
  }

  const max = Math.max(...entries.map(([, v]) => v));

  return (
    <div className="space-y-3">
      {entries.map(([label, value]) => {
        const pct = max > 0 ? (value / max) * 100 : 0;
        return (
          <div key={label}>
            <div className="flex justify-between text-sm mb-1">
              <span className="text-gray-300 font-mono text-xs">{label}</span>
              <span className="text-gray-400 text-xs">
                {formatValue ? formatValue(value) : value}
              </span>
            </div>
            <div className="w-full bg-gray-800 rounded-full h-2">
              <div
                className={`${color} h-2 rounded-full transition-all`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
