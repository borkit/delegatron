"use client";

import { useEffect, useState } from "react";
import type { RunSummary, SystemHealth } from "@/lib/api";
import { listRuns, getHealth, getSystemHealth, getServerDefaults } from "@/lib/api";
import RunCard from "@/components/RunCard";

export default function DashboardHome() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [version, setVersion] = useState<string>("");
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [timezone, setTimezone] = useState<"utc" | "local">("utc");
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const [runsData, healthData, fullHealth, defaults] = await Promise.all([
          listRuns(),
          getHealth(),
          getSystemHealth().catch(() => null),
          getServerDefaults().catch(() => null),
        ]);
        setRuns(runsData);
        setVersion(healthData.version);
        setHealth(fullHealth);
        if (defaults?.log_timezone === "utc" || defaults?.log_timezone === "local") {
          setTimezone(defaults.log_timezone);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to connect to API");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const totalRuns = runs.length;
  const approvedRuns = runs.filter((r) => r.status === "approved").length;
  const totalCost = runs.reduce((sum, r) => sum + (r.total_cost_usd || 0), 0);
  const activeRuns = runs.filter(
    (r) => r.status === "running" || r.status === "in-progress"
  ).length;
  const recentRuns = [...runs].reverse().slice(0, 5);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-1">
            {version ? `Delegatron v${version}` : "Delegatron"}
          </p>
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 mb-6 text-red-400 text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <p className="text-gray-500">Loading...</p>
      ) : (
        <>
          {/* System Health */}
          {health && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
              <HealthPill label="Server" ok={true} detail={`v${health.version}`} />
              <HealthPill label="Ollama" ok={health.ollama.running} detail={health.ollama.running ? `${health.ollama.models.length} models` : "offline"} />
              <HealthPill label="Docker" ok={health.docker.available} detail={health.docker.available ? `v${health.docker.version}` : "not found"} />
              <HealthPill label="Disk" ok={health.disk.used_percent < 90} detail={`${health.disk.free_gb} GB free (${health.disk.used_percent}%)`} />
            </div>
          )}

          {/* Stats */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4 mb-8">
            <StatCard label="Total Runs" value={totalRuns.toString()} />
            <StatCard label="Approved" value={approvedRuns.toString()} accent="text-green-400" />
            <StatCard label="Active" value={activeRuns.toString()} accent="text-blue-400" />
            <StatCard label="Total Cost" value={`$${totalCost.toFixed(2)}`} accent="text-yellow-400" />
            <a href="/compliance" className="bg-gray-900 border border-gray-800 rounded-lg p-4 hover:border-green-800 transition-colors group">
              <p className="text-xs text-gray-500 uppercase tracking-wider">Zero-Cloud</p>
              <p className="text-2xl font-bold mt-1 text-green-400 group-hover:text-green-300">
                {totalCost === 0 && totalRuns > 0 ? "Verified" : totalRuns === 0 ? "--" : "View"}
              </p>
            </a>
          </div>

          <h2 className="text-lg font-semibold text-white mb-4">
            Recent Runs
          </h2>
          {recentRuns.length === 0 ? (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
              <p className="text-gray-400 mb-2">No runs yet</p>
              <p className="text-gray-600 text-sm mb-4">Create your first pipeline run to get started.</p>
              <a href="/new" className="inline-block px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors">
                New Run
              </a>
            </div>
          ) : (
            <div className="space-y-3">
              {recentRuns.map((run, i) => (
                <RunCard key={run.run_id || `run-${i}`} run={run} timezone={timezone} />
              ))}
            </div>
          )}
        </>
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

function HealthPill({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm ${
      ok ? "bg-green-900/10 border-green-900/30" : "bg-red-900/10 border-red-900/30"
    }`}>
      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${ok ? "bg-green-500" : "bg-red-500"}`} />
      <span className={ok ? "text-green-400" : "text-red-400"}>{label}</span>
      <span className="text-gray-500 text-xs ml-auto">{detail}</span>
    </div>
  );
}
