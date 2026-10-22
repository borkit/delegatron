"use client";

import { Suspense, useState, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { listRuns, getComplianceReport, getServerDefaults, type RunSummary, type ComplianceReport } from "@/lib/api";
import Link from "next/link";

export default function CompliancePage() {
  return (
    <Suspense fallback={<p className="text-gray-500">Loading...</p>}>
      <ComplianceContent />
    </Suspense>
  );
}

function ComplianceContent() {
  const searchParams = useSearchParams();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [report, setReport] = useState<ComplianceReport | null>(null);
  const [selectedRun, setSelectedRun] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [timezone, setTimezone] = useState<"utc" | "local">("utc");

  async function loadRuns() {
    try {
      const defaults = await getServerDefaults().catch(() => null);
      if (defaults?.log_timezone === "utc" || defaults?.log_timezone === "local") {
        setTimezone(defaults.log_timezone);
      }
      const data = await listRuns();
      const completed = data.filter(
        (r) => r.status !== "running" && r.status !== "started"
      );
      setRuns(completed);
      setRunsLoaded(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load runs");
    }
  }

  async function loadReport(runId: string) {
    setLoading(true);
    setError("");
    setSelectedRun(runId);
    try {
      const data = await getComplianceReport(runId);
      setReport(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "No compliance report available");
      setReport(null);
    }
    setLoading(false);
  }

  // Load runs on mount
  useEffect(() => {
    loadRuns();
  }, []);

  // Auto-load report if ?run= query param is present
  useEffect(() => {
    const runParam = searchParams.get("run");
    if (runParam && runsLoaded && !selectedRun) {
      loadReport(runParam);
    }
  }, [searchParams, runsLoaded]);

  return (
    <div className="max-w-5xl">
      <h1 className="text-2xl font-bold text-white mb-2">Zero-Cloud Compliance</h1>
      <p className="text-sm text-gray-500 mb-6">
        Audit which network destinations each pipeline run used: LLM endpoints, sandbox and git push.
        Non-local destinations are flagged; nothing is blocked. Each report carries a SHA-256
        integrity checksum, not a signature.
      </p>

      {/* Run selector */}
      <div className="flex items-center gap-3 mb-6">
        <select
          value={selectedRun}
          onChange={(e) => e.target.value && loadReport(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 min-w-[300px]"
        >
          <option value="">Select a completed run...</option>
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {r.run_id.slice(0, 8)} — {r.status} — {r.request?.slice(0, 50) || "no description"}
            </option>
          ))}
        </select>
        {loading && <span className="text-xs text-blue-400 animate-pulse">Loading...</span>}
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-400 text-sm mb-6">
          {error}
        </div>
      )}

      {report && (
        <div className="space-y-6">
          {/* Verdict banner */}
          <div
            className={`rounded-xl p-6 border ${
              report.cloud_free
                ? "bg-green-900/20 border-green-800"
                : "bg-red-900/20 border-red-800"
            }`}
          >
            <div className="flex items-center gap-4">
              <div
                className={`w-16 h-16 rounded-full flex items-center justify-center text-3xl font-bold ${
                  report.cloud_free
                    ? "bg-green-900/50 text-green-400"
                    : "bg-red-900/50 text-red-400"
                }`}
              >
                {report.cloud_free ? "\u2713" : "\u2717"}
              </div>
              <div>
                <h2
                  className={`text-xl font-bold ${
                    report.cloud_free ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {report.cloud_free
                    ? "ZERO-CLOUD VERIFIED"
                    : `CLOUD ACCESS DETECTED (${report.violations.length} violation${report.violations.length !== 1 ? "s" : ""})`}
                </h2>
                <p className="text-sm text-gray-400 mt-1">
                  Run {report.run_id.slice(0, 8)} &mdash;{" "}
                  {timezone === "utc"
                    ? new Date(report.timestamp).toLocaleString(undefined, { timeZone: "UTC" }) + " UTC"
                    : new Date(report.timestamp).toLocaleString()}
                </p>
              </div>
            </div>
          </div>

          {/* Summary stats */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <StatCard label="Total Events" value={report.summary.total_events} />
            <StatCard
              label="Local Events"
              value={report.summary.local_events}
              color="text-green-400"
            />
            <StatCard
              label="Cloud Events"
              value={report.summary.cloud_events}
              color={report.summary.cloud_events > 0 ? "text-red-400" : "text-green-400"}
            />
            <StatCard label="Mode" value={report.summary.enforcement_mode} />
          </div>

          {/* Provider breakdown */}
          <Section title="Provider Breakdown">
            <div className="grid grid-cols-2 gap-2">
              {Object.entries(report.summary.by_provider).map(([provider, count]) => (
                <div
                  key={provider}
                  className="flex justify-between items-center bg-gray-800/50 rounded-lg px-4 py-2"
                >
                  <span className="text-sm text-gray-300 font-mono">{provider}</span>
                  <span className="text-sm text-gray-500">{count} call{count !== 1 ? "s" : ""}</span>
                </div>
              ))}
            </div>
          </Section>

          {/* Violations */}
          {report.violations.length > 0 && (
            <Section title="Violations">
              <div className="space-y-2">
                {report.violations.map((v, i) => (
                  <div
                    key={i}
                    className="bg-red-900/10 border border-red-900/30 rounded-lg p-3"
                  >
                    <div className="flex items-center gap-2 text-sm">
                      <span className="text-red-400 font-medium">{v.category}</span>
                      <span className="text-gray-600">|</span>
                      <span className="text-gray-400">{v.role}</span>
                      <span className="text-gray-600">|</span>
                      <span className="text-gray-400 font-mono">{v.provider} &rarr; {v.host}</span>
                    </div>
                    <p className="text-xs text-red-400/70 mt-1">{v.reason}</p>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {/* Event log */}
          <Section title="Event Log">
            <div className="max-h-80 overflow-y-auto space-y-1">
              {report.events.map((evt, i) => (
                <div
                  key={i}
                  className="flex items-center gap-3 text-xs py-1.5 px-3 rounded hover:bg-gray-800/50"
                >
                  <span
                    className={`w-2 h-2 rounded-full ${
                      evt.is_local ? "bg-green-500" : "bg-red-500"
                    }`}
                  />
                  <span className="text-gray-500 font-mono w-20">{evt.category}</span>
                  <span className="text-gray-400 w-24">{evt.role}</span>
                  <span className="text-gray-300 font-mono">{evt.provider}</span>
                  <span className="text-gray-600 ml-auto font-mono">{evt.host}</span>
                </div>
              ))}
            </div>
          </Section>

          {/* Fingerprint */}
          <Section title="Report Fingerprint">
            <div className="flex items-center gap-3">
              <code className="text-xs text-gray-400 font-mono bg-gray-800 px-3 py-2 rounded-lg break-all">
                SHA-256: {report.fingerprint}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(report.fingerprint)}
                className="text-xs text-blue-400 hover:text-blue-300 whitespace-nowrap"
              >
                Copy
              </button>
            </div>
            <p className="text-xs text-gray-600 mt-2">
              SHA-256 of the whole report (without this field). Recompute it from the stored
              zero_cloud_report.json to spot accidental edits; it is a checksum, not a signature,
              so it cannot prove the report was not rewritten.
            </p>
          </Section>

          {/* Export */}
          <div className="flex gap-3">
            <Link
              href={`/runs/${report.run_id}`}
              className="px-4 py-2 text-sm bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors"
            >
              View Run Details
            </Link>
            <button
              onClick={() => {
                const blob = new Blob([JSON.stringify(report, null, 2)], {
                  type: "application/json",
                });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `compliance-${report.run_id.slice(0, 8)}.json`;
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="px-4 py-2 text-sm bg-blue-600/20 text-blue-400 border border-blue-800 rounded-lg hover:bg-blue-600/30 transition-colors"
            >
              Export JSON Report
            </button>
          </div>
        </div>
      )}

      {!report && !error && !loading && (
        <div className="text-center py-20 text-gray-600">
          <p className="text-lg mb-2">Select a completed run to view its compliance report</p>
          <p className="text-sm">
            Zero-cloud reports are generated automatically for every pipeline run.
          </p>
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  color = "text-white",
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-xl font-bold mt-1 ${color}`}>{value}</p>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-gray-900/50 border border-gray-800 rounded-lg p-5">
      <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider mb-3">
        {title}
      </h3>
      {children}
    </div>
  );
}
