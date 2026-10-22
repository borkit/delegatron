"use client";

import { useEffect, useState, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import type { RunSummary, IterationSummary, ResumeLimits } from "@/lib/api";
import { getRun, getRunFiles, cancelRun, approveRun, rejectRun, retryRun, resumeRun, getAuthenticatedSSEUrl, getSystemStats, getIterations } from "@/lib/api";
import { useToast } from "@/components/Toast";
import StatusBadge from "@/components/StatusBadge";
import IterationTimeline from "@/components/IterationTimeline";
import LiveThinking from "@/components/LiveThinking";
import RetryDialog from "@/components/RetryDialog";
import Link from "next/link";

interface LiveEvent {
  event: string;
  [key: string]: unknown;
}

// Final statuses POST /resume accepts: the run stopped before every task was done.
const RESUMABLE_STATUSES = ["error", "cancelled", "stalled", "exhausted"];

function useRunId(): string {
  // Prefer Next.js params (works in dev), fall back to URL path (static export)
  const params = useParams();
  if (params?.id && params.id !== "_") return params.id as string;
  if (typeof window !== "undefined") {
    const parts = window.location.pathname.replace(/\/+$/, "").split("/");
    return parts[parts.length - 1] || "";
  }
  return "";
}

export default function RunDetailClient() {
  const router = useRouter();
  const runId = useRunId();
  const [run, setRun] = useState<RunSummary | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [pendingPlan, setPendingPlan] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [retryOpen, setRetryOpen] = useState(false);
  const [iterations, setIterations] = useState<IterationSummary[]>([]);
  // Bumped when the run is resumed: it keeps its id, so reload it and its event stream.
  const [session, setSession] = useState(0);
  const eventsEndRef = useRef<HTMLDivElement>(null);
  const { toast } = useToast();

  // Load iterations when run finishes or on mount for completed runs
  function loadIterations() {
    getIterations(runId)
      .then((data) => setIterations(data.iterations))
      .catch(() => {}); // silently fail — iterations are supplementary
  }

  // Load run data and auto-refresh
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      while (!cancelled) {
        try {
          const data = await getRun(runId);
          if (!cancelled) setRun(data);
          if (data.status !== "running" && data.status !== "started" && data.iterations !== 0) {
            loadIterations();
            break;
          }
        } catch (e) {
          if (!cancelled) setError(e instanceof Error ? e.message : "Failed to fetch");
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    getRun(runId)
      .then((d) => {
        setRun(d);
        setLoading(false);
        // Load iterations immediately for completed runs
        if (d.status !== "running" && d.status !== "started" && d.iterations !== 0) {
          loadIterations();
        }
        poll();
      })
      .catch((e) => { setError(e instanceof Error ? e.message : "Failed"); setLoading(false); });
    return () => { cancelled = true; };
  }, [runId, session]);

  // SSE event stream — batches token events to avoid per-token re-renders
  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;
    const pending: LiveEvent[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | null = null;

    function flush() {
      flushTimer = null;
      if (pending.length === 0) return;
      const batch = pending.splice(0);
      setEvents((prev) => {
        const updated = [...prev, ...batch];
        return updated.length > 1000 ? updated.slice(-1000) : updated;
      });
    }

    getAuthenticatedSSEUrl(`/api/runs/${runId}/events`).then((sseUrl) => {
      if (cancelled) return;
      es = new EventSource(sseUrl);
    es.onmessage = (msg) => {
      try {
        const evt: LiveEvent = JSON.parse(msg.data);
        pending.push(evt);

        // Non-token events flush immediately for responsiveness
        if (evt.event !== "token") {
          if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
          flush();
        } else if (!flushTimer) {
          // Batch token events — flush every 500ms
          flushTimer = setTimeout(flush, 500);
        }

        // Check for plan pending approval
        if (evt.event === "plan_pending_approval" && evt.plan) {
          setPendingPlan(evt.plan as Record<string, unknown>);
        }
        if (evt.event === "plan_approved" || evt.event === "plan_rejected") {
          setPendingPlan(null);
        }

        // Refresh run data on key events
        if (["pipeline_complete", "sandbox_complete", "reviewer_complete", "complete"].includes(evt.event)) {
          getRun(runId).then(setRun).catch(() => {});
        }
      } catch { /* ignore parse errors */ }
    };
    es.onerror = () => {
      es?.close();
      setEvents((prev) => [...prev, { event: "stream_disconnected" }]);
    };
    });
    return () => {
      cancelled = true;
      es?.close();
      if (flushTimer) clearTimeout(flushTimer);
    };
  }, [runId, session]);

  // Auto-scroll events container (not the page) on new non-token events
  const prevEventCountRef = useRef(0);
  useEffect(() => {
    const nonTokenCount = events.filter((e) => e.event !== "token").length;
    if (nonTokenCount > prevEventCountRef.current) {
      prevEventCountRef.current = nonTokenCount;
      // Scroll only the events container, not the whole page
      const container = eventsEndRef.current?.parentElement;
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    }
  }, [events]);

  async function handleApprove() {
    try {
      await approveRun(runId);
      setPendingPlan(null);
      toast("Plan approved — pipeline continuing", "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to approve plan", "error");
    }
  }
  async function handleReject() {
    try {
      await rejectRun(runId);
      setPendingPlan(null);
      toast("Plan rejected", "info");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to reject plan", "error");
    }
  }
  async function handleRetryConfirm(opts: { freshStart: boolean; preset?: string }) {
    setRetryOpen(false);
    setSubmitting(true);
    try {
      const data = await retryRun(runId, {
        freshStart: opts.freshStart,
        preset: opts.preset,
      });
      const msg = data.strategy === "refine"
        ? "Retry started — fixing review issues"
        : "Retry started — fresh run";
      toast(opts.preset ? `${msg} (${opts.preset})` : msg, "success");
      router.push(`/runs/${data.run_id}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Retry failed", "error");
      setSubmitting(false);
    }
  }
  async function handleResume() {
    if (submitting || !run) return;
    let limits: ResumeLimits | undefined;
    if (run.status === "exhausted") {
      // The run used up its limits: without a higher one it would stop again at once.
      const answer = window.prompt(
        "This run reached its iteration or time limit. Max iterations for the resumed run " +
          "(1-100; leave blank to keep the current limit):",
        "10",
      );
      if (answer === null) return;
      if (answer.trim()) {
        const maxIterations = Number(answer);
        if (!Number.isInteger(maxIterations) || maxIterations < 1 || maxIterations > 100) {
          toast("Enter a whole number from 1 to 100", "error");
          return;
        }
        limits = { max_iterations: maxIterations };
      }
    }
    setSubmitting(true);
    try {
      const data = await resumeRun(runId, limits);
      toast("Run resumed from its last checkpoint", "success");
      if (data.run_id === runId) {
        // Same run, new session: reload the run and reconnect its event stream.
        setEvents([]);
        prevEventCountRef.current = 0;
        setIterations([]);
        setSubmitting(false);
        setSession((s) => s + 1);
      } else {
        router.push(`/runs/${data.run_id}`);
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : "Resume failed", "error");
      setSubmitting(false);
    }
  }

  if (loading) return <p className="text-gray-500">Loading...</p>;
  if (error) return <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-400 text-sm">{error}</div>;
  if (!run) return null;

  const isActive = run.status === "running" || run.status === "started" || (run.iterations === 0 && !run.error);

  return (
    <div className="max-w-4xl">
      <Link href="/runs" className="text-sm text-gray-500 hover:text-gray-300">&larr; Back to runs</Link>

      <div className="flex items-center gap-4 mt-4 mb-6">
        <h1 className="text-2xl font-bold text-white font-mono">{run.run_id}</h1>
        <StatusBadge status={run.status} />
        {isActive && (
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-blue-400 animate-pulse">Running...</span>
            <button
              onClick={async () => { try { await cancelRun(runId); toast("Run cancelled", "info"); } catch (e) { toast(e instanceof Error ? e.message : "Failed to cancel", "error"); } }}
              className="px-3 py-1.5 text-xs font-medium bg-red-600/20 text-red-400 border border-red-800 rounded-lg hover:bg-red-600/30 transition-colors"
              title="Stop the running pipeline immediately"
            >
              Cancel
            </button>
          </div>
        )}
        {!isActive && (
          <div className="ml-auto flex gap-2">
            {RESUMABLE_STATUSES.includes(run.status) && (
              <button
                onClick={handleResume}
                disabled={submitting}
                className="px-3 py-1.5 text-xs font-medium bg-green-600/20 text-green-400 border border-green-800 rounded-lg hover:bg-green-600/30 transition-colors disabled:opacity-50"
                title="Continue this run from its last checkpoint, in its own workspace and branch: finished tasks and their files are kept and the run keeps its ID. Needs the run's workspace (use Retry if it was deleted)."
              >
                Resume
              </button>
            )}
            <button
              onClick={() => setRetryOpen(true)}
              disabled={submitting}
              className="px-3 py-1.5 text-xs font-medium bg-blue-600/20 text-blue-400 border border-blue-800 rounded-lg hover:bg-blue-600/30 transition-colors disabled:opacity-50"
              title="Retry this run — optionally change preset/models"
            >
              {submitting ? "Starting..." : "Retry"}
            </button>
            <button
              onClick={async () => {
                const url = await getAuthenticatedSSEUrl(`/api/runs/${runId}/download`);
                window.open(url, "_blank");
              }}
              className="px-3 py-1.5 text-xs font-medium bg-purple-600/20 text-purple-400 border border-purple-800 rounded-lg hover:bg-purple-600/30 transition-colors"
              title="Download all generated code as a ZIP archive"
            >
              Download Code
            </button>
            <Link
              href={`/compliance?run=${runId}`}
              className="px-3 py-1.5 text-xs font-medium bg-green-600/20 text-green-400 border border-green-800 rounded-lg hover:bg-green-600/30 transition-colors"
              title="View zero-cloud verification and SOC 2 compliance report for this run"
            >
              Compliance
            </Link>
          </div>
        )}
      </div>

      {run.request && (
        <Section title="Request">
          <p className="text-gray-300">{run.request}</p>
        </Section>
      )}

      {/* Plan Approval */}
      {pendingPlan && (
        <Section title="Plan Approval Required">
          <div className="space-y-4">
            <p className="text-gray-300 font-medium text-lg">{(pendingPlan.summary as string) || ""}</p>

            {/* Tasks */}
            <div>
              <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">Tasks ({(pendingPlan.tasks as Array<Record<string, string>>)?.length || 0})</p>
              {(pendingPlan.tasks as Array<Record<string, string>>)?.map((t, i) => (
                <div key={i} className="flex gap-2 text-sm py-1">
                  <span className={`text-xs px-1.5 py-0.5 rounded ${
                    t.risk_level === "high" ? "bg-red-900/30 text-red-400" :
                    t.risk_level === "medium" ? "bg-yellow-900/30 text-yellow-400" :
                    "bg-green-900/30 text-green-400"
                  }`}>{t.risk_level || "low"}</span>
                  <span className="text-gray-300">{t.title}</span>
                </div>
              ))}
            </div>

            {/* Cost Estimate */}
            {(pendingPlan as any).cost_estimate && (
              <div className="bg-gray-800 rounded-lg p-3">
                <p className="text-xs text-gray-500 uppercase tracking-wider mb-2">Estimated Cost</p>
                <div className="grid grid-cols-3 gap-3 text-sm">
                  <div>
                    <p className="text-gray-500 text-xs">Best case (1 iter)</p>
                    <p className="text-green-400 font-bold">${(pendingPlan as any).cost_estimate.estimated_min}</p>
                  </div>
                  <div>
                    <p className="text-gray-500 text-xs">Worst case ({(pendingPlan as any).cost_estimate.max_iterations} iter)</p>
                    <p className="text-yellow-400 font-bold">${(pendingPlan as any).cost_estimate.estimated_max}</p>
                  </div>
                  <div>
                    <p className="text-gray-500 text-xs">Per iteration</p>
                    <p className="text-gray-300 font-bold">${(pendingPlan as any).cost_estimate.per_iteration_cost}</p>
                  </div>
                </div>
                <div className="mt-2 text-xs text-gray-600">
                  Models: {(pendingPlan as any).cost_estimate.models?.architect} / {(pendingPlan as any).cost_estimate.models?.implementer} / {(pendingPlan as any).cost_estimate.models?.reviewer}
                </div>
              </div>
            )}

            {/* Assumptions & Questions */}
            {(pendingPlan.assumptions as string[])?.length > 0 && (
              <div className="text-sm">
                <p className="text-xs text-gray-500 uppercase mb-1">Assumptions</p>
                {(pendingPlan.assumptions as string[]).map((a, i) => (
                  <p key={i} className="text-gray-400 text-xs">- {a}</p>
                ))}
              </div>
            )}

            <div className="flex gap-3 pt-2 border-t border-gray-800">
              <button
                onClick={handleApprove}
                className="px-6 py-2.5 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-500"
                title="Approve this plan and begin the implement/test/review cycle"
              >
                Approve &amp; Start
              </button>
              <button
                onClick={handleReject}
                className="px-6 py-2.5 bg-red-600/20 text-red-400 text-sm font-medium rounded-lg border border-red-800 hover:bg-red-600/30"
                title="Reject this plan and stop the pipeline"
              >
                Reject
              </button>
            </div>
          </div>
        </Section>
      )}

      {run.plan_summary && (
        <Section title="Plan">
          <p className="text-gray-300 whitespace-pre-wrap">{run.plan_summary}</p>
        </Section>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
        <DetailCard label="Status" value={run.status} />
        <DetailCard label="Iterations" value={run.iterations?.toString() || "0"} />
        <DetailCard label="Total Cost" value={`$${(run.total_cost_usd || 0).toFixed(4)}`} />
        <DetailCard label="Duration" value={`${(run.elapsed_seconds || 0).toFixed(1)}s`} />
      </div>

      {/* Progress Bar */}
      <ProgressBar events={events} isActive={isActive} />

      {/* Live Thinking — real-time model reasoning stream */}
      <LiveThinking events={events} isActive={isActive} />

      {/* System Stats (shown during active runs) */}
      {isActive && <LiveSystemStats />}

      {/* Iteration History */}
      {iterations.length > 0 && (
        <IterationTimeline
          runId={runId}
          iterations={iterations}
          isActive={isActive}
          onRestore={() => {
            getRun(runId).then(setRun).catch(() => {});
            loadIterations();
            toast("Restored to earlier iteration", "success");
          }}
        />
      )}

      {/* Live Events — token events are shown in LiveThinking above */}
      {(() => {
        const displayed = events.filter((e) => e.event !== "token");
        return (
          <Section title={`Pipeline Events (${displayed.length})`}>
            <div className="max-h-[500px] overflow-y-auto space-y-2 text-sm">
              {displayed.length === 0 && isActive && (
                <p className="text-gray-600 animate-pulse">Waiting for events...</p>
              )}
              {displayed.map((evt, i) => (
                <EventRow key={i} evt={evt} index={i} />
              ))}
              <div ref={eventsEndRef} />
            </div>
          </Section>
        );
      })()}

      {/* Generated Code Viewer */}
      {(run as any).files_created?.length > 0 && (
        <CodeViewer runId={runId} fileNames={(run as any).files_created} />
      )}

      {/* Sandbox Info (extracted from events) */}
      <SandboxInfo events={events} />

      {/* Test Results */}
      {(run as any).test_report && (
        <Section title="Test Results">
          <div className="flex items-center gap-3 mb-2">
            <span className={`w-3 h-3 rounded-full ${(run as any).test_report.passed ? "bg-green-500" : "bg-red-500"}`} />
            <span className={`font-medium ${(run as any).test_report.passed ? "text-green-400" : "text-red-400"}`}>
              {(run as any).test_report.passed ? "PASSED" : "FAILED"}
            </span>
            <span className="text-gray-500 text-sm">
              {(run as any).test_report.total} tests, {(run as any).test_report.failures} failures
            </span>
          </div>
          {(run as any).test_report.stderr && !(run as any).test_report.passed && (
            <pre className="text-xs text-red-400 bg-red-900/20 rounded p-2 mt-2 overflow-x-auto whitespace-pre-wrap">
              {(run as any).test_report.stderr}
            </pre>
          )}
        </Section>
      )}

      {/* Review */}
      {(run as any).review_details && (
        <Section title="Review">
          <p className="text-gray-300 text-sm mb-3">{(run as any).review_details.summary}</p>
          {(run as any).review_details.issues?.length > 0 && (
            <div className="space-y-2">
              {(run as any).review_details.issues.map((issue: any, i: number) => (
                <div key={i} className={`text-sm px-2 py-1 rounded ${
                  issue.severity === "blocker" ? "bg-red-900/20 text-red-400" :
                  issue.severity === "major" ? "bg-yellow-900/20 text-yellow-400" :
                  "bg-gray-800 text-gray-400"
                }`}>
                  <span className="font-medium uppercase text-xs">{issue.severity}</span>: {issue.title || issue.description || ""}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* Cost Breakdown */}
      {(run as any).cost_breakdown?.by_role && (
        <Section title="Cost Breakdown">
          <div className="grid grid-cols-3 gap-3">
            {Object.entries((run as any).cost_breakdown.by_role as Record<string, any>).map(([role, data]) => (
              <div key={role} className="bg-gray-800 rounded-lg p-3">
                <div className="flex items-center gap-2">
                  <p className="text-xs text-gray-500 uppercase tracking-wider">{role}</p>
                  {data.is_local && (
                    <span className="text-[9px] px-1 py-0.5 rounded bg-purple-900/40 text-purple-400" title="Running locally on your GPU — cost estimated from electricity and hardware wear">LOCAL</span>
                  )}
                </div>
                <p className="text-lg font-bold text-white mt-1">${(data.cost_usd as number)?.toFixed(4)}</p>
                <p className="text-xs text-gray-600 mt-1">{data.calls} calls, {data.input_tokens}+{data.output_tokens} tokens</p>
              </div>
            ))}
          </div>
          {/* Local cost detail */}
          {(run as any).cost_breakdown?.local_cost && (
            <div className="mt-3 bg-purple-900/10 border border-purple-900/30 rounded-lg p-3">
              <p className="text-[10px] text-purple-400 uppercase tracking-wider mb-2 font-medium" title="Estimated real cost of running local models based on GPU power draw and electricity rates">Local GPU Cost Estimate</p>
              <div className="grid grid-cols-4 gap-3 text-xs">
                <div>
                  <p className="text-gray-500">Electricity</p>
                  <p className="text-purple-300 font-bold" title="Cost of electricity consumed by the GPU during model inference">${(run as any).cost_breakdown.local_cost.electricity_usd?.toFixed(6)}</p>
                </div>
                <div>
                  <p className="text-gray-500">Hardware wear</p>
                  <p className="text-purple-300 font-bold" title="Amortized GPU depreciation based on purchase price and expected lifetime">${(run as any).cost_breakdown.local_cost.hardware_usd?.toFixed(6)}</p>
                </div>
                <div>
                  <p className="text-gray-500">GPU time</p>
                  <p className="text-purple-300 font-bold">{((run as any).cost_breakdown.local_cost.generation_seconds || 0).toFixed(1)}s</p>
                </div>
                <div>
                  <p className="text-gray-500">Total local</p>
                  <p className="text-purple-300 font-bold">${(run as any).cost_breakdown.local_cost.total_local_usd?.toFixed(6)}</p>
                </div>
              </div>
              <p className="text-[10px] text-gray-600 mt-2">Configure: DELEGATRON_ELECTRICITY_RATE ($/kWh), DELEGATRON_GPU_TDP_WATTS, DELEGATRON_GPU_PRICE_USD</p>
            </div>
          )}
        </Section>
      )}

      {run.is_approved !== undefined && run.status !== "running" && run.status !== "started" && (
        <Section title="Verdict">
          <div className="flex items-center gap-2">
            <span className={`w-3 h-3 rounded-full ${run.is_approved ? "bg-green-500" : "bg-red-500"}`} />
            <span className={`font-medium ${run.is_approved ? "text-green-400" : "text-red-400"}`}>
              {run.is_approved ? "Approved" : "Not Approved"}
            </span>
          </div>
        </Section>
      )}

      {run.error && (
        <Section title="Error">
          <pre className="text-red-400 text-sm bg-red-900/20 rounded p-3 overflow-x-auto whitespace-pre-wrap">{run.error}</pre>
        </Section>
      )}

      <RetryDialog
        open={retryOpen}
        meta={(run as any)._meta || null}
        onConfirm={handleRetryConfirm}
        onCancel={() => setRetryOpen(false)}
      />
    </div>
  );
}

function EventRow({ evt, index }: { evt: LiveEvent; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const e = evt.event;
  const iter = evt.iteration as number | undefined;

  // Event icon and color
  const icon = EVENT_ICONS[e] || "\u2022";
  const color = getEventColor(e);

  // Collect detail fields (everything except "event" itself)
  const detailKeys = Object.keys(evt).filter(
    (k) => k !== "event" && evt[k] !== undefined && evt[k] !== null && evt[k] !== ""
  );
  const hasDetails = detailKeys.length > 0;

  return (
    <div className={`rounded ${color}`}>
      <div
        className={`flex items-start gap-3 py-2 px-3 ${hasDetails ? "cursor-pointer hover:bg-white/5" : ""}`}
        onClick={() => hasDetails && setExpanded(!expanded)}
        onKeyDown={(e) => { if (hasDetails && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); setExpanded(!expanded); } }}
        role={hasDetails ? "button" : undefined}
        tabIndex={hasDetails ? 0 : undefined}
        aria-expanded={hasDetails ? expanded : undefined}
      >
        <span className="text-base flex-shrink-0 w-5 text-center">{icon}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-gray-200">{EVENT_LABELS[e] || e}</span>
            {iter !== undefined && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 font-mono">
                iter {iter}
              </span>
            )}
            {/* Status badges */}
            {evt.status === "ok" && <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-400">OK</span>}
            {evt.passed === true && <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-400">PASSED ({String(evt.total)} tests)</span>}
            {evt.passed === false && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/40 text-red-400">FAILED {evt.total ? `(${evt.total} tests)` : "(no tests collected)"}</span>}
            {evt.accepted === true && <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-400">ACCEPTED</span>}
            {evt.accepted === false && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/40 text-red-400">REJECTED</span>}
            {/* Expand indicator */}
            {hasDetails && (
              <span className="text-gray-600 text-xs ml-auto">
                {expanded ? "\u25BC" : "\u25B6"} {detailKeys.length} field{detailKeys.length !== 1 ? "s" : ""}
              </span>
            )}
          </div>
          {/* EMCS score detail (always visible) */}
          {e === "emcs_score" && (
            <div className="flex items-center gap-3 mt-1 text-xs">
              <span className={`font-bold ${(evt.score as number) >= 0.8 ? "text-green-400" : (evt.score as number) >= 0.5 ? "text-yellow-400" : "text-red-400"}`}>
                Score: {(evt.score as number)?.toFixed(2)}
              </span>
              <span className="text-gray-600">threshold: {(evt.threshold as number)?.toFixed(1)}</span>
              {evt.issue_counts != null && typeof evt.issue_counts === "object" && Object.keys(evt.issue_counts as object).length > 0 ? (
                <span className="text-gray-500">
                  issues: {Object.entries(evt.issue_counts as Record<string, number>).map(([k, v]) => `${v} ${k}`).join(", ")}
                </span>
              ) : null}
              {(evt.acceptable as boolean) && <span className="text-green-400 font-medium">PASS</span>}
            </div>
          )}
          {/* EMCS override: the score approved what the reviewer rejected */}
          {e === "emcs_override" && (
            <div className="flex items-center gap-3 mt-1 text-xs">
              <span className="font-bold text-yellow-400">Score: {(evt.score as number)?.toFixed(2)}</span>
              <span className="text-gray-600">threshold: {(evt.threshold as number)?.toFixed(2)}</span>
              <span className="text-gray-500">approved despite the reviewer&apos;s rejection</span>
            </div>
          )}
          {/* Architect reasoning preview */}
          {e === "architect_reasoning" && (
            <div className="mt-1 text-xs">
              <p className="text-gray-300">{String(evt.summary || "")}</p>
              {!expanded && Array.isArray(evt.tasks) && (evt.tasks as Array<Record<string, string>>).length > 0 && (
                <p className="text-gray-500 mt-0.5">
                  {(evt.tasks as Array<Record<string, string>>).length} task{(evt.tasks as Array<Record<string, string>>).length !== 1 ? "s" : ""}:
                  {" "}{(evt.tasks as Array<Record<string, string>>).slice(0, 3).map((t) => t.title).join(", ")}
                  {(evt.tasks as Array<Record<string, string>>).length > 3 ? "..." : ""}
                </p>
              )}
            </div>
          )}
          {/* Implementer reasoning preview */}
          {e === "implementer_reasoning" && (
            <div className="mt-1 text-xs">
              <p className="text-gray-300">{String(evt.rationale || "").slice(0, 150)}{String(evt.rationale || "").length > 150 ? "..." : ""}</p>
              {!expanded && Array.isArray(evt.files_changed) && (evt.files_changed as string[]).length > 0 && (
                <p className="text-gray-500 mt-0.5 font-mono">
                  {(evt.files_changed as string[]).join(", ")}
                </p>
              )}
            </div>
          )}
          {/* Reviewer reasoning preview */}
          {e === "reviewer_reasoning" && (
            <div className="mt-1 text-xs">
              <p className="text-gray-300">{String(evt.summary || "")}</p>
              {!expanded && Array.isArray(evt.issues) && (evt.issues as Array<Record<string, string>>).length > 0 && (
                <div className="flex gap-2 mt-0.5 flex-wrap">
                  {(evt.issues as Array<Record<string, string>>).map((iss, j) => (
                    <span
                      key={j}
                      className={`px-1.5 py-0.5 rounded text-[10px] ${
                        iss.severity === "blocker" ? "bg-red-900/30 text-red-400" :
                        iss.severity === "major" ? "bg-yellow-900/30 text-yellow-400" :
                        "bg-gray-800 text-gray-500"
                      }`}
                    >
                      {iss.severity}: {iss.title}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
          {/* Pipeline complete summary (always visible) */}
          {e === "pipeline_complete" && (
            <div className="flex items-center gap-4 mt-1 text-xs text-gray-500">
              <span>{String(evt.iterations)} iterations</span>
              <span>${(evt.total_cost_usd as number)?.toFixed(4)}</span>
              <span>{(evt.elapsed_seconds as number)?.toFixed(1)}s</span>
            </div>
          )}
          {/* Error detail (always visible) */}
          {evt.error != null && !expanded ? (
            <p className="text-xs text-red-400 mt-1 truncate">{String(evt.error).slice(0, 200)}</p>
          ) : null}
        </div>
      </div>

      {/* Expanded detail panel */}
      {expanded && (
        <div className="mx-3 mb-2 ml-11 border-t border-gray-800 pt-2 space-y-1.5">
          {detailKeys.map((key) => (
            <EventDetailField key={key} fieldKey={key} value={evt[key]} />
          ))}
        </div>
      )}
    </div>
  );
}

function EventDetailField({ fieldKey, value }: { fieldKey: string; value: unknown }) {
  const isLong = typeof value === "string" && value.length > 120;
  const isObject = typeof value === "object" && value !== null;

  return (
    <div className="text-xs">
      <span className="text-gray-500 font-mono">{fieldKey}: </span>
      {isObject ? (
        <pre className="text-gray-400 bg-gray-800/50 rounded p-2 mt-1 overflow-x-auto whitespace-pre-wrap max-h-60 overflow-y-auto">
          {JSON.stringify(value, null, 2)}
        </pre>
      ) : isLong ? (
        <pre className="text-gray-400 bg-gray-800/50 rounded p-2 mt-1 overflow-x-auto whitespace-pre-wrap max-h-60 overflow-y-auto">
          {String(value)}
        </pre>
      ) : (
        <span className={`${fieldKey === "error" ? "text-red-400" : "text-gray-300"}`}>
          {String(value)}
        </span>
      )}
    </div>
  );
}

const EVENT_ICONS: Record<string, string> = {
  pipeline_start: "\uD83D\uDE80",
  started: "\uD83D\uDE80",
  queued: "\u23F3",
  pipeline_executing: "\u25B6\uFE0F",
  architect_start: "\uD83E\uDDE0",
  architect_complete: "\uD83E\uDDE0",
  architect_reasoning: "\uD83E\uDDE0",
  implementer_start: "\uD83D\uDCBB",
  implementer_complete: "\uD83D\uDCBB",
  implementer_reasoning: "\uD83D\uDCBB",
  reviewer_start: "\uD83D\uDD0E",
  reviewer_complete: "\uD83D\uDD0E",
  reviewer_reasoning: "\uD83D\uDD0E",
  iteration_start: "\uD83D\uDD04",
  sandbox_starting: "\uD83D\uDCE6",
  sandbox_complete: "\uD83D\uDCE6",
  sandbox_skipped: "\u23ED\uFE0F",
  tests_complete: "\uD83E\uDDEA",
  plan_saved: "\uD83D\uDCCB",
  plan_created: "\uD83D\uDCCB",
  plan_pending_approval: "\u23F3",
  plan_approved: "\u2705",
  plan_rejected: "\u274C",
  patch_saved: "\uD83D\uDCDD",
  patch_generated: "\uD83D\uDCDD",
  test_report_saved: "\uD83E\uDDEA",
  review_saved: "\uD83D\uDD0D",
  review_complete: "\uD83D\uDD0D",
  emcs_score: "\uD83D\uDCCA",
  emcs_override: "\uD83D\uDCCA",
  run_resumed: "\u25B6\uFE0F",
  task_advanced: "\u2705",
  task_skipped: "\u23ED\uFE0F",
  implementer_pass: "\uD83D\uDCBB",
  constraint_violation: "\u26A0\uFE0F",
  secrets_detected: "\uD83D\uDD12",
  new_dependencies: "\uD83D\uDCE6",
  approved: "\u2705",
  exhausted: "\u23F9\uFE0F",
  cancelled: "\u274C",
  complete: "\uD83C\uDFC1",
  pipeline_complete: "\uD83C\uDFC1",
  error: "\u274C",
  terminated: "\u23F9\uFE0F",
  stream_disconnected: "\u26A0\uFE0F",
  parallel_start: "\u2194\uFE0F",
  parallel_complete: "\u2194\uFE0F",
  parallel_task_fallback: "\u21A9\uFE0F",
  parallel_patch_conflict: "\u26A0\uFE0F",
  parallel_disabled: "\u23F8\uFE0F",
};

const EVENT_LABELS: Record<string, string> = {
  pipeline_start: "Pipeline Started",
  started: "Run Started",
  queued: "Queued for GPU",
  pipeline_executing: "Pipeline Executing",
  architect_start: "Architect Starting",
  architect_complete: "Architect Complete",
  architect_reasoning: "Architect Thinking",
  implementer_start: "Implementer Starting",
  implementer_complete: "Implementer Complete",
  implementer_reasoning: "Implementer Thinking",
  reviewer_start: "Reviewer Starting",
  reviewer_complete: "Reviewer Complete",
  reviewer_reasoning: "Reviewer Thinking",
  iteration_start: "Iteration Started",
  sandbox_starting: "Sandbox Starting",
  sandbox_complete: "Sandbox Complete",
  sandbox_skipped: "Sandbox Skipped",
  tests_complete: "Tests Complete",
  plan_saved: "Plan Created",
  plan_created: "Plan Created",
  plan_pending_approval: "Awaiting Approval",
  plan_approved: "Plan Approved",
  plan_rejected: "Plan Rejected",
  patch_saved: "Code Generated",
  patch_generated: "Code Generated",
  test_report_saved: "Tests Executed",
  review_saved: "Code Reviewed",
  review_complete: "Review Complete",
  emcs_score: "Quality Score",
  emcs_override: "Approved by Quality Score (reviewer rejected)",
  run_resumed: "Run Resumed from Checkpoint",
  task_advanced: "Task Complete — Next Task",
  task_skipped: "Task Skipped",
  implementer_pass: "Implementer Pass",
  constraint_violation: "Constraint Violation",
  secrets_detected: "Secrets Detected",
  new_dependencies: "New Dependencies",
  approved: "Approved",
  exhausted: "Max Retries Reached",
  cancelled: "Cancelled",
  complete: "Pipeline Complete",
  pipeline_complete: "Pipeline Complete",
  error: "Error",
  terminated: "Terminated",
  stream_disconnected: "Event stream disconnected",
  parallel_start: "Parallel Tasks Starting",
  parallel_complete: "Parallel Tasks Complete",
  parallel_task_fallback: "Parallel Task Deferred",
  parallel_patch_conflict: "Parallel Patch Conflict",
  parallel_disabled: "Parallel Mode Disabled",
};

function getEventColor(event: string): string {
  if (event.includes("error") || event === "terminated") return "bg-red-900/20";
  if (event === "pipeline_complete") return "bg-green-900/20";
  if (event.includes("approved") || event === "emcs_score") return "bg-gray-800/50";
  if (event.includes("pending") || event.includes("rejected")) return "bg-yellow-900/20";
  // EMCS approved what the reviewer rejected: worth a second look.
  if (event === "emcs_override") return "bg-yellow-900/20";
  if (event === "secrets_detected" || event === "constraint_violation") return "bg-orange-900/20";
  return "bg-gray-900/30";
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-2">{title}</h2>
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">{children}</div>
    </div>
  );
}

function DetailCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className="text-lg font-bold text-white mt-1">{value}</p>
    </div>
  );
}

function Sparkline({ data, color, max }: { data: number[]; color: string; max?: number }) {
  if (data.length < 2) return null;
  const peak = max || Math.max(...data, 1);
  const w = 120;
  const h = 24;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - (v / peak) * h;
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg width={w} height={h} className="inline-block">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

const MAX_HISTORY = 30;

function LiveSystemStats() {
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);
  const [vramHistory, setVramHistory] = useState<number[]>([]);
  const [containerHistory, setContainerHistory] = useState<number[]>([]);

  useEffect(() => {
    function poll() {
      getSystemStats().then((s) => {
        setStats(s);
        const gpu = (s.gpu || {}) as Record<string, unknown>;
        const docker = (s.docker || {}) as Record<string, unknown>;
        setVramHistory((prev) => [...prev.slice(-(MAX_HISTORY - 1)), (gpu.total_vram_gb as number) || 0]);
        setContainerHistory((prev) => [...prev.slice(-(MAX_HISTORY - 1)), (docker.containers_running as number) || 0]);
      }).catch(() => {});
    }
    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, []);

  if (!stats) return null;

  const gpu = (stats.gpu || {}) as Record<string, unknown>;
  const docker = (stats.docker || {}) as Record<string, unknown>;
  const models = (gpu.models || []) as Array<Record<string, unknown>>;
  const containers = (docker.containers || []) as Array<Record<string, unknown>>;

  return (
    <div className="grid grid-cols-2 gap-3 mb-6">
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-400">GPU / Ollama</span>
            {models.length > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/50 text-green-400 border border-green-800">
                {gpu.total_vram_gb as number}GB
              </span>
            )}
          </div>
          <Sparkline data={vramHistory} color="#22c55e" max={16} />
        </div>
        {models.length === 0 ? (
          <p className="text-xs text-gray-600">No models loaded</p>
        ) : (
          <div className="space-y-1">
            {models.map((m, i) => (
              <div key={i} className="flex items-center justify-between text-xs">
                <span className="text-gray-300 font-mono truncate">{m.name as string}</span>
                <span className="text-gray-500 ml-2">{m.size_vram_gb as number}GB</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-gray-400">Docker</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">
              {docker.mode as string}
            </span>
          </div>
          <Sparkline data={containerHistory} color="#3b82f6" max={5} />
        </div>
        {containers.length === 0 ? (
          <p className="text-xs text-gray-600">No containers running</p>
        ) : (
          <div className="space-y-1">
            {containers.map((c, i) => (
              <div key={i} className="text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-gray-300 font-mono truncate">{c.image as string}</span>
                  <span className="text-gray-500 ml-2">{c.status as string}</span>
                </div>
                {(String(c["cpu"] ?? "") || String(c["mem"] ?? "")) && (
                  <div className="flex gap-3 mt-0.5 text-[10px] text-gray-500">
                    {c["cpu"] ? <span>CPU: {String(c["cpu"])}</span> : null}
                    {c["mem"] ? <span>MEM: {String(c["mem"])}</span> : null}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ProgressBar({ events, isActive }: { events: LiveEvent[]; isActive: boolean }) {
  // Find the latest progress event (search from end, avoid full array copy)
  const progress = [...events].reverse().find((e) => e.event === "progress");
  if (!progress && !isActive) return null;
  if (!progress) {
    return (
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-gray-500">Starting pipeline...</span>
        </div>
        <div className="w-full bg-gray-800 rounded-full h-2">
          <div className="bg-blue-500 h-2 rounded-full w-1 animate-pulse" />
        </div>
      </div>
    );
  }

  const pct = progress.percent as number || 0;
  const phase = String(progress.phase || "");
  const iter = progress.iteration as number || 0;
  const maxIter = progress.max_iterations as number || 5;
  const elapsed = progress.elapsed_seconds as number || 0;
  const tokens = progress.total_tokens as number || 0;
  const cost = progress.total_cost_usd as number || 0;
  const eta = progress.estimated_remaining_seconds as number | null;
  const complete = !isActive;

  const phaseLabels: Record<string, string> = {
    architect: "Architect planning...",
    architect_done: "Plan ready",
    implementer: "Implementer writing code...",
    sandbox: "Sandbox running tests...",
    reviewer: "Reviewer evaluating...",
    done: "Complete",
  };

  return (
    <div className="mb-6">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-400">
            {complete ? "Complete" : phaseLabels[phase] || phase}
          </span>
          {iter > 0 && !complete && (
            <span className="text-[10px] text-gray-600 font-mono">
              iter {iter}/{maxIter}
            </span>
          )}
        </div>
        <div className="flex items-center gap-4 text-[10px] text-gray-500 font-mono">
          <span>{Math.floor(elapsed / 60)}m {Math.round(elapsed % 60)}s</span>
          {tokens > 0 && <span>{tokens.toLocaleString()} tok</span>}
          <span>${cost.toFixed(4)}</span>
          {eta != null && !complete && (
            <span className="text-blue-400">~{Math.ceil(eta / 60)}m left</span>
          )}
        </div>
      </div>
      <div className="w-full bg-gray-800 rounded-full h-2">
        <div
          className={`h-2 rounded-full transition-all duration-500 ${complete ? "bg-green-500" : "bg-blue-500"}`}
          style={{ width: `${complete ? 100 : pct}%` }}
        />
      </div>
    </div>
  );
}

function SandboxInfo({ events }: { events: LiveEvent[] }) {
  // Find the latest sandbox_starting and sandbox_complete events
  const starting = [...events].reverse().find((e) => e.event === "sandbox_starting");
  const complete = [...events].reverse().find((e) => e.event === "sandbox_complete");
  const skipped = events.find((e) => e.event === "sandbox_skipped");

  if (!starting && !complete && !skipped) return null;

  if (skipped) {
    return (
      <Section title="Sandbox">
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <span className="w-2 h-2 rounded-full bg-gray-600" />
          Skipped — {String(skipped.reason || "non-testable language")}
        </div>
      </Section>
    );
  }

  const image = String(complete?.image || starting?.image || "");
  const containerOs = String(complete?.container_os || starting?.container_os || "linux");
  const shell = String(complete?.shell || starting?.shell || "bash");
  const imageSize = String(complete?.image_size || "");
  const networkIsolation = complete?.network_isolation ?? starting?.network_isolation ?? true;
  const pullInfo = complete?.pull as Record<string, unknown> | undefined;
  const testMs = complete?.test_ms as number | undefined;
  const installMs = complete?.install_ms as number | undefined;
  const isRunning = starting && !complete;

  return (
    <Section title="Sandbox">
      <div className="space-y-3">
        {/* Status indicator */}
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${isRunning ? "bg-blue-500 animate-pulse" : complete?.passed ? "bg-green-500" : "bg-red-500"}`} />
          <span className="text-sm font-medium text-gray-200">
            {isRunning ? "Running..." : "Complete"}
          </span>
        </div>

        {/* Container details grid */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {image && (
            <div className="bg-gray-800/50 rounded-lg px-3 py-2">
              <p className="text-[10px] text-gray-500 uppercase">Image</p>
              <p className="text-xs text-gray-300 font-mono truncate" title={image}>{image.split("/").pop()}</p>
            </div>
          )}
          <div className="bg-gray-800/50 rounded-lg px-3 py-2">
            <p className="text-[10px] text-gray-500 uppercase">OS</p>
            <p className="text-xs text-gray-300">{containerOs === "windows" ? "Windows" : "Linux"}</p>
          </div>
          <div className="bg-gray-800/50 rounded-lg px-3 py-2">
            <p className="text-[10px] text-gray-500 uppercase">Shell</p>
            <p className="text-xs text-gray-300 font-mono">{shell}</p>
          </div>
          {imageSize && imageSize !== "unknown" && (
            <div className="bg-gray-800/50 rounded-lg px-3 py-2">
              <p className="text-[10px] text-gray-500 uppercase">Image Size</p>
              <p className="text-xs text-gray-300">{imageSize}</p>
            </div>
          )}
          <div className="bg-gray-800/50 rounded-lg px-3 py-2">
            <p className="text-[10px] text-gray-500 uppercase">Network</p>
            <p className={`text-xs ${networkIsolation ? "text-green-400" : "text-yellow-400"}`}>
              {networkIsolation ? "Isolated" : "Connected"}
            </p>
          </div>
          {installMs != null && installMs > 0 && (
            <div className="bg-gray-800/50 rounded-lg px-3 py-2">
              <p className="text-[10px] text-gray-500 uppercase">Install</p>
              <p className="text-xs text-gray-300">{(installMs / 1000).toFixed(1)}s</p>
            </div>
          )}
          {testMs != null && (
            <div className="bg-gray-800/50 rounded-lg px-3 py-2">
              <p className="text-[10px] text-gray-500 uppercase">Test</p>
              <p className="text-xs text-gray-300">{(testMs / 1000).toFixed(1)}s</p>
            </div>
          )}
          {pullInfo && pullInfo.pulled === true && (
            <div className="bg-gray-800/50 rounded-lg px-3 py-2">
              <p className="text-[10px] text-gray-500 uppercase">Image Pull</p>
              <p className="text-xs text-yellow-400">{((pullInfo.duration_ms as number) / 1000).toFixed(1)}s</p>
            </div>
          )}
        </div>

        {/* Pull warning */}
        {pullInfo && pullInfo.status === "pull_failed" && (
          <div className="text-xs text-red-400 bg-red-900/20 rounded p-2">
            Image pull failed: {String(pullInfo.error || "unknown error")}
          </div>
        )}
      </div>
    </Section>
  );
}

function CodeViewer({ runId, fileNames }: { runId: string; fileNames: string[] }) {
  const [files, setFiles] = useState<Record<string, string> | null>(null);
  const [activeFile, setActiveFile] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadFiles() {
    setLoading(true);
    try {
      const data = await getRunFiles(runId);
      setFiles(data);
      const keys = Object.keys(data);
      if (keys.length > 0) setActiveFile(keys[0]);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load files");
    }
    setLoading(false);
  }

  // File extension to rough language hint
  function getLang(path: string): string {
    const ext = path.split(".").pop() || "";
    const map: Record<string, string> = {
      py: "python", js: "javascript", ts: "typescript", tsx: "tsx",
      jsx: "jsx", json: "json", md: "markdown", yml: "yaml",
      yaml: "yaml", toml: "toml", html: "html", css: "css",
      sh: "bash", rs: "rust", go: "go", java: "java",
    };
    return map[ext] || ext;
  }

  return (
    <Section title={`Generated Code (${fileNames.length} file${fileNames.length !== 1 ? "s" : ""})`}>
      {!files && !loading && !loadError && (
        <button
          onClick={loadFiles}
          className="px-4 py-2 text-sm bg-blue-600/20 text-blue-400 border border-blue-800 rounded-lg hover:bg-blue-600/30 transition-colors"
        >
          View generated code
        </button>
      )}
      {loading && <p className="text-gray-500 text-sm">Loading files...</p>}
      {loadError && <p className="text-red-400 text-sm">{loadError}</p>}
      {files && (
        <div>
          {/* File tabs */}
          <div className="flex gap-1 mb-3 overflow-x-auto pb-1">
            {Object.keys(files).map((path) => (
              <button
                key={path}
                onClick={() => setActiveFile(path)}
                className={`px-3 py-1.5 text-xs font-mono rounded-md whitespace-nowrap transition-colors ${
                  activeFile === path
                    ? "bg-blue-600/20 text-blue-400 border border-blue-800"
                    : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                }`}
              >
                {path.split("/").pop()}
              </button>
            ))}
          </div>
          {/* File path */}
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs text-gray-500 font-mono">{activeFile}</span>
            <span className="text-[10px] text-gray-600 uppercase">{getLang(activeFile)}</span>
          </div>
          {/* Code content */}
          <pre className="text-xs text-gray-300 bg-gray-950 border border-gray-800 rounded-lg p-4 overflow-auto max-h-[500px] whitespace-pre font-mono leading-relaxed">
            {files[activeFile] || "// empty file"}
          </pre>
        </div>
      )}
    </Section>
  );
}
