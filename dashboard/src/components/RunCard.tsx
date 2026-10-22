import Link from "next/link";
import type { RunSummary } from "@/lib/api";
import StatusBadge from "./StatusBadge";

interface RunCardProps {
  run: RunSummary;
  timezone?: "utc" | "local";
}

function formatDate(iso: string, tz: "utc" | "local"): string {
  const d = new Date(iso);
  if (tz === "utc") {
    return d.toLocaleString(undefined, { timeZone: "UTC" }) + " UTC";
  }
  return d.toLocaleString();
}

export default function RunCard({ run, timezone = "utc" }: RunCardProps) {
  if (!run?.run_id) return null;
  const title = run.request || run.plan_summary || "Untitled run";
  const shortId = run.run_id;

  return (
    <Link href={`/runs/${run.run_id}`} title={`View run ${shortId} — ${run.status}`}>
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 hover:border-gray-700 transition-colors cursor-pointer">
        <div className="flex items-center justify-between mb-1">
          <p className="text-sm font-medium text-white line-clamp-2 flex-1 mr-4">
            {title}
          </p>
          <StatusBadge status={run.status} />
        </div>
        <div className="flex items-center gap-4 text-xs text-gray-500 mt-2">
          <code className="font-mono">{shortId}</code>
          {run.created_at && (
            <span>{formatDate(run.created_at, timezone)}</span>
          )}
          {run.iterations !== undefined && (
            <span>{run.iterations} iter</span>
          )}
          {run.total_cost_usd !== undefined && (
            <span>${run.total_cost_usd.toFixed(4)}</span>
          )}
          {run.elapsed_seconds !== undefined && (
            <span>{run.elapsed_seconds.toFixed(1)}s</span>
          )}
        </div>
      </div>
    </Link>
  );
}
