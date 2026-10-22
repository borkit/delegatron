"use client";

import { useState } from "react";
import type { IterationSummary, IterationDiff } from "@/lib/api";
import { getIterationDiff, restoreIteration } from "@/lib/api";
import ConfirmDialog from "./ConfirmDialog";

interface Props {
  runId: string;
  iterations: IterationSummary[];
  isActive: boolean;
  onRestore?: () => void;
}

export default function IterationTimeline({ runId, iterations, isActive, onRestore }: Props) {
  const [selected, setSelected] = useState<number | null>(null);
  const [diff, setDiff] = useState<IterationDiff | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [restoreTarget, setRestoreTarget] = useState<number | null>(null);
  const [restoring, setRestoring] = useState(false);

  async function loadDiff(iteration: number) {
    if (selected === iteration && diff) {
      setSelected(null);
      setDiff(null);
      return;
    }
    setSelected(iteration);
    setLoading(true);
    setError("");
    try {
      const data = await getIterationDiff(runId, iteration);
      setDiff(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load diff");
      setDiff(null);
    }
    setLoading(false);
  }

  async function handleRestore() {
    if (restoreTarget === null) return;
    setRestoring(true);
    try {
      await restoreIteration(runId, restoreTarget);
      onRestore?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Restore failed");
    }
    setRestoring(false);
    setRestoreTarget(null);
  }

  if (iterations.length === 0) {
    return null;
  }

  return (
    <div className="mb-6">
      <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">
        Iteration History ({iterations.length})
      </h2>

      {/* Timeline */}
      <div className="relative">
        {/* Connecting line */}
        <div className="absolute left-[18px] top-4 bottom-4 w-px bg-gray-800" />

        <div className="space-y-1">
          {iterations.map((iter) => {
            const isSelected = selected === iter.iteration;
            const testsPassed = iter.tests?.passed ?? null;
            const hasBlockers = (iter.review?.issue_counts?.blocker ?? 0) > 0;
            const hasMajors = (iter.review?.issue_counts?.major ?? 0) > 0;

            // Status indicator color
            let dotColor = "bg-gray-600";
            if (iter.is_approved) dotColor = "bg-green-500";
            else if (testsPassed === false || hasBlockers) dotColor = "bg-red-500";
            else if (hasMajors) dotColor = "bg-yellow-500";
            else if (testsPassed === true) dotColor = "bg-blue-500";

            return (
              <div key={iter.iteration}>
                <div
                  className={`flex items-center gap-3 px-3 py-2.5 rounded-lg cursor-pointer transition-colors ${
                    isSelected ? "bg-gray-800 border border-gray-700" : "hover:bg-gray-900/50"
                  }`}
                  onClick={() => loadDiff(iter.iteration)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); loadDiff(iter.iteration); } }}
                  role="button"
                  tabIndex={0}
                  aria-expanded={isSelected}
                >
                  {/* Dot */}
                  <div
                    className={`w-[9px] h-[9px] rounded-full ${dotColor} shrink-0 ring-2 ring-gray-950 z-10`}
                    title={
                      iter.is_approved ? "Approved — all gates passed" :
                      testsPassed === false || hasBlockers ? "Failed — tests or blockers" :
                      hasMajors ? "Has major issues" :
                      testsPassed === true ? "Tests passing, review pending" :
                      "Pending"
                    }
                  />

                  {/* Iteration label */}
                  <span className="text-xs font-mono text-gray-400 w-10 shrink-0">
                    #{iter.iteration}
                  </span>

                  {/* Status badges */}
                  <div className="flex items-center gap-2 flex-1 min-w-0 flex-wrap">
                    {/* Tests */}
                    {iter.tests && (
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded ${
                          iter.tests.passed
                            ? "bg-green-900/40 text-green-400"
                            : "bg-red-900/40 text-red-400"
                        }`}
                        title={`Sandbox tests: ${iter.tests.total} total, ${iter.tests.failures} failed`}
                      >
                        {iter.tests.passed ? "PASS" : "FAIL"} {iter.tests.total}t
                      </span>
                    )}

                    {/* Review */}
                    {iter.review && (
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded ${
                          iter.is_approved
                            ? "bg-green-900/40 text-green-400"
                            : iter.review.reviewer_accepted
                            ? "bg-yellow-900/40 text-yellow-400"
                            : "bg-gray-800 text-gray-500"
                        }`}
                        title={
                          iter.is_approved
                            ? "Reviewer accepted and all hard gates passed (tests, no blockers)"
                            : iter.review.reviewer_accepted
                            ? "Reviewer accepted but hard gates blocked approval (failed tests or blocker/major issues)"
                            : `Reviewer rejected — ${iter.review.summary || "see details"}`
                        }
                      >
                        {iter.is_approved
                          ? "Approved"
                          : iter.review.reviewer_accepted
                          ? "Gated"
                          : "Rejected"}
                        {Object.keys(iter.review.issue_counts).length > 0 && (
                          <> ({Object.entries(iter.review.issue_counts).map(([k, v]) => `${v}${k[0]}`).join("/")})</>
                        )}
                      </span>
                    )}

                    {/* Patch */}
                    {!iter.patch_applied && iter.files_changed && iter.files_changed.length > 0 && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-red-900/30 text-red-400"
                        title="The generated patch could not be applied cleanly to the workspace"
                      >
                        patch failed
                      </span>
                    )}

                    {/* Files */}
                    {iter.files_changed && iter.files_changed.length > 0 && (
                      <span
                        className="text-[10px] text-gray-600 truncate"
                        title={`Files changed:\n${iter.files_changed.join("\n")}`}
                      >
                        {iter.files_changed.length} file{iter.files_changed.length !== 1 ? "s" : ""}
                      </span>
                    )}

                    {/* Rationale preview */}
                    {iter.rationale && (
                      <span className="text-[10px] text-gray-600 truncate max-w-[200px]" title={iter.rationale}>
                        {iter.rationale}
                      </span>
                    )}
                  </div>

                  {/* Cost */}
                  <span
                    className="text-[10px] text-gray-600 font-mono shrink-0"
                    title={`Cumulative cost through iteration #${iter.iteration}`}
                  >
                    ${(iter.total_cost_usd || 0).toFixed(4)}
                  </span>

                  {/* Expand indicator */}
                  <span className="text-gray-600 text-xs shrink-0">
                    {isSelected ? "\u25BC" : "\u25B6"}
                  </span>
                </div>

                {/* Expanded detail panel */}
                {isSelected && (
                  <div className="ml-11 mt-1 mb-2">
                    {loading && <p className="text-gray-500 text-sm py-2">Loading diff...</p>}
                    {error && <p className="text-red-400 text-sm py-2">{error}</p>}
                    {diff && !loading && (
                      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                        {/* Action bar */}
                        <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800">
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-gray-400">
                              {Object.keys(diff.files).length} file{Object.keys(diff.files).length !== 1 ? "s" : ""}
                            </span>
                            {diff.rationale && (
                              <span className="text-xs text-gray-500 truncate max-w-[300px]">
                                — {diff.rationale}
                              </span>
                            )}
                          </div>
                          {!isActive && (
                            <button
                              onClick={(e) => { e.stopPropagation(); setRestoreTarget(iter.iteration); }}
                              className="px-2.5 py-1 text-[10px] font-medium bg-yellow-600/20 text-yellow-400 border border-yellow-800 rounded hover:bg-yellow-600/30 transition-colors"
                              title="Roll back the run summary to this iteration's checkpoint state"
                            >
                              Restore to #{iter.iteration}
                            </button>
                          )}
                        </div>

                        {/* File tabs + diff content */}
                        <IterationDiffViewer diff={diff} />

                        {/* Test report if available */}
                        {diff.test_report && (
                          <div className="border-t border-gray-800 px-3 py-2">
                            <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Tests</p>
                            <div className="flex items-center gap-2 text-xs">
                              <span className={`w-2 h-2 rounded-full ${diff.test_report.passed ? "bg-green-500" : "bg-red-500"}`} />
                              <span className={diff.test_report.passed ? "text-green-400" : "text-red-400"}>
                                {diff.test_report.passed ? "PASSED" : "FAILED"}
                              </span>
                              <span className="text-gray-500">
                                {String(diff.test_report.total || 0)} tests, {String(diff.test_report.failures || 0)} failures
                              </span>
                            </div>
                            {diff.test_report.stderr && !diff.test_report.passed && (
                              <pre className="text-[10px] text-red-400 bg-red-900/20 rounded p-2 mt-1.5 overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto">
                                {String(diff.test_report.stderr).slice(-500)}
                              </pre>
                            )}
                          </div>
                        )}

                        {/* Review report if available */}
                        {diff.review_report && (
                          <div className="border-t border-gray-800 px-3 py-2">
                            <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Review</p>
                            <p className="text-xs text-gray-300 mb-1">{String(diff.review_report.summary || "")}</p>
                            {Array.isArray(diff.review_report.issues) && (diff.review_report.issues as Array<Record<string, string>>).length > 0 && (
                              <div className="flex gap-1.5 flex-wrap">
                                {(diff.review_report.issues as Array<Record<string, string>>).map((iss, j) => (
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
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <ConfirmDialog
        open={restoreTarget !== null}
        title={`Restore to iteration #${restoreTarget}`}
        message={`This resets the run's workspace to the commit of iteration #${restoreTarget}; downloads and the code view will show that iteration's files. The run keeps its final status. Continue?`}
        confirmLabel={restoring ? "Restoring..." : "Restore"}
        variant="danger"
        onConfirm={handleRestore}
        onCancel={() => setRestoreTarget(null)}
      />
    </div>
  );
}

function IterationDiffViewer({ diff }: { diff: IterationDiff }) {
  const [activeFile, setActiveFile] = useState(() => {
    const keys = Object.keys(diff.files);
    return keys.length > 0 ? keys[0] : "";
  });
  const [viewMode, setViewMode] = useState<"code" | "diff">("diff");

  const fileNames = Object.keys(diff.files);
  const hasDiff = diff.diff_unified.length > 0;

  if (fileNames.length === 0 && !hasDiff) {
    return <p className="text-gray-500 text-xs p-3">No code changes in this iteration.</p>;
  }

  return (
    <div>
      {/* View mode toggle + file tabs */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-gray-800 overflow-x-auto">
        {hasDiff && (
          <div className="flex gap-1 mr-2 shrink-0">
            <button
              onClick={() => setViewMode("diff")}
              className={`px-2 py-0.5 text-[10px] rounded ${
                viewMode === "diff" ? "bg-blue-600/30 text-blue-400" : "text-gray-500 hover:text-gray-300"
              }`}
              title="Show unified diff — additions in green, deletions in red"
            >
              Diff
            </button>
            <button
              onClick={() => setViewMode("code")}
              className={`px-2 py-0.5 text-[10px] rounded ${
                viewMode === "code" ? "bg-blue-600/30 text-blue-400" : "text-gray-500 hover:text-gray-300"
              }`}
              title="Show full file contents for each changed file"
            >
              Files
            </button>
          </div>
        )}
        {viewMode === "code" && fileNames.map((path) => (
          <button
            key={path}
            onClick={() => setActiveFile(path)}
            className={`px-2 py-0.5 text-[10px] font-mono rounded whitespace-nowrap ${
              activeFile === path
                ? "bg-blue-600/20 text-blue-400"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {path.split("/").pop()}
          </button>
        ))}
      </div>

      {/* Content */}
      {viewMode === "diff" && hasDiff ? (
        <pre className="text-[11px] leading-relaxed p-3 overflow-auto max-h-[400px] font-mono">
          {diff.diff_unified.split("\n").map((line, i) => {
            let color = "text-gray-400";
            if (line.startsWith("+") && !line.startsWith("+++")) color = "text-green-400";
            else if (line.startsWith("-") && !line.startsWith("---")) color = "text-red-400";
            else if (line.startsWith("@@")) color = "text-blue-400";
            else if (line.startsWith("diff ") || line.startsWith("index ")) color = "text-gray-600";
            return (
              <div key={i} className={`${color} ${
                line.startsWith("+") && !line.startsWith("+++") ? "bg-green-900/10" :
                line.startsWith("-") && !line.startsWith("---") ? "bg-red-900/10" : ""
              }`}>
                {line}
              </div>
            );
          })}
        </pre>
      ) : (
        <pre className="text-[11px] text-gray-300 p-3 overflow-auto max-h-[400px] font-mono leading-relaxed whitespace-pre">
          {diff.files[activeFile] || "// empty file"}
        </pre>
      )}
    </div>
  );
}
