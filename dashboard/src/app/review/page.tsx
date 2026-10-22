"use client";

import { useState, useEffect, useRef } from "react";
import { reviewCodebase, type ReviewResponse } from "@/lib/api";

function ReviewProgress({ mode }: { mode: "code" | "logs" }) {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef(Date.now());

  useEffect(() => {
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - startRef.current) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 mb-6">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-white flex items-center gap-2">
          <span className="inline-block w-2 h-2 bg-blue-500 rounded-full animate-pulse" />
          {mode === "code" ? "Reviewing code..." : "Analyzing logs..."}
        </h3>
        <span className="text-xs text-gray-500 font-mono">{elapsed}s</span>
      </div>
      <p className="text-xs text-gray-500">
        {mode === "code"
          ? "Scanning workspace, gathering context, running reviewer. Typically 60-300s. Do not close this page."
          : "Running reviewer agent on log content. Typically 60-180s. Do not close this page."}
      </p>
      <div className="mt-3 h-1 bg-gray-800 rounded-full overflow-hidden">
        <div className="h-full bg-blue-600 rounded-full animate-pulse" style={{ width: "100%" }} />
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const [mode, setMode] = useState<"code" | "logs">("code");
  const [workspace, setWorkspace] = useState("");
  const [logContent, setLogContent] = useState("");
  const [focus, setFocus] = useState("all");
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ReviewResponse | null>(null);

  async function handleReview() {
    if (mode === "code" && !workspace.trim()) {
      setError("Enter a workspace path or git URL");
      return;
    }
    if (mode === "logs" && !logContent.trim()) {
      setError("Paste log content or upload a log file");
      return;
    }
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const body: Record<string, unknown> = {
        focus: focus === "all" ? undefined : focus,
      };
      if (question.trim()) body.question = question.trim();
      if (mode === "logs") body.log_content = logContent.trim();
      else body.workspace = workspace.trim();
      const data = await reviewCodebase(body as any);
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Review failed");
    }
    setLoading(false);
  }

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-bold text-white mb-2">Review</h1>
      <p className="text-sm text-gray-500 mb-6">
        Review code or analyze logs. Read-only — no changes made.
      </p>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 mb-6 space-y-4">
        <div className="flex gap-2">
          <button
            onClick={() => setMode("code")}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              mode === "code" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
            }`}
          >
            Review Code
          </button>
          <button
            onClick={() => setMode("logs")}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              mode === "logs" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
            }`}
          >
            Analyze Logs
          </button>
        </div>

        {mode === "code" ? (
          <div>
            <label className="block text-xs text-gray-500 mb-1">Repository</label>
            <input
              type="text"
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500"
              placeholder="/path/to/repo or https://github.com/org/repo.git"
            />
            <p className="text-xs text-gray-600 mt-1">
              Local path or git URL. Works best on medium-sized repos (10-100 source files).
            </p>
          </div>
        ) : (
          <div>
            <label className="block text-xs text-gray-500 mb-1">Log Content</label>
            <textarea
              value={logContent}
              onChange={(e) => setLogContent(e.target.value)}
              rows={10}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-blue-500 resize-y"
              placeholder="Paste log output here, or upload a log file..."
            />
            <div className="flex items-center gap-3 mt-1">
              <p className="text-xs text-gray-600">Paste logs or upload a file</p>
              <label className="text-xs text-blue-400 hover:text-blue-300 cursor-pointer">
                Upload file
                <input
                  type="file"
                  accept=".log,.txt,.out,.err"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) file.text().then(setLogContent);
                  }}
                />
              </label>
            </div>
          </div>
        )}

        <div>
          <label className="block text-xs text-gray-500 mb-1">What to look for (optional)</label>
          <input
            type="text"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            placeholder={
              mode === "logs"
                ? "e.g. Why does it disconnect? Find authentication failures..."
                : "e.g. Check for SQL injection, review error handling..."
            }
          />
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Focus Area</label>
          <div className="flex gap-2">
            {(["all", "security", "bugs", "performance"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFocus(f)}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                  focus === f ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>

        <button
          onClick={handleReview}
          disabled={loading}
          className="px-6 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-500 disabled:opacity-50 transition-colors"
        >
          {loading ? "Running..." : mode === "code" ? "Start Review" : "Analyze Logs"}
        </button>
      </div>

      {loading && <ReviewProgress mode={mode} />}

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-400 text-sm mb-6">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-6">
          <div
            className={`rounded-xl p-6 border ${
              result.accepted
                ? "bg-green-900/20 border-green-800"
                : "bg-red-900/20 border-red-800"
            }`}
          >
            <div className="flex items-center gap-3 mb-3">
              <span
                className={`w-3 h-3 rounded-full ${
                  result.accepted ? "bg-green-500" : "bg-red-500"
                }`}
              />
              <span
                className={`text-lg font-bold ${
                  result.accepted ? "text-green-400" : "text-red-400"
                }`}
              >
                {result.accepted
                  ? "No Issues Found"
                  : `${result.issues.length} Issue${result.issues.length !== 1 ? "s" : ""} Found`}
              </span>
              {result.files_reviewed > 0 && (
                <span className="text-xs text-gray-500">
                  · {result.files_reviewed} file{result.files_reviewed !== 1 ? "s" : ""} reviewed
                </span>
              )}
            </div>
            <p className="text-sm text-gray-300 whitespace-pre-wrap">{result.summary}</p>
          </div>

          {result.issues.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-lg font-semibold text-white">Issues</h2>
              {result.issues.map((issue, i) => {
                const severityColor: Record<string, string> = {
                  blocker: "bg-red-900/40 border-red-700 text-red-300",
                  major: "bg-orange-900/40 border-orange-700 text-orange-300",
                  minor: "bg-yellow-900/40 border-yellow-700 text-yellow-300",
                  info: "bg-blue-900/40 border-blue-700 text-blue-300",
                };
                const color =
                  severityColor[issue.severity] || "bg-gray-800 border-gray-700 text-gray-300";
                return (
                  <div key={i} className={`rounded-lg p-4 border ${color}`}>
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-bold uppercase">{issue.severity}</span>
                    </div>
                    <p className="text-sm font-medium text-white mb-1">{issue.title}</p>
                    {issue.details && (
                      <p className="text-xs text-gray-400 whitespace-pre-wrap">{issue.details}</p>
                    )}
                    {issue.recommended_fix && (
                      <div className="mt-2 pt-2 border-t border-gray-700">
                        <p className="text-xs font-medium text-gray-300 mb-1">Recommended fix:</p>
                        <p className="text-xs text-gray-400 whitespace-pre-wrap">
                          {issue.recommended_fix}
                        </p>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
