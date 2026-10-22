"use client";

import { useEffect, useRef, useState } from "react";

interface LiveEvent {
  event: string;
  [key: string]: unknown;
}

interface Props {
  events: LiveEvent[];
  isActive: boolean;
}

interface RoleBuffer {
  /** Raw accumulated token text */
  raw: string;
  /** Extracted readable reasoning (from <think> blocks) */
  reasoning: string;
  /** Human-readable fields extracted from partial JSON */
  fields: ParsedField[];
  active: boolean;
  collapsed: boolean;
}

interface ParsedField {
  label: string;
  value: string;
}

const ROLE_LABELS: Record<string, string> = {
  architect: "Architect",
  implementer: "Implementer",
  reviewer: "Reviewer",
};

const ROLE_COLORS: Record<string, { text: string; bg: string; border: string; dot: string }> = {
  architect: { text: "text-blue-400", bg: "bg-blue-900/10", border: "border-blue-900/30", dot: "bg-blue-400" },
  implementer: { text: "text-green-400", bg: "bg-green-900/10", border: "border-green-900/30", dot: "bg-green-400" },
  reviewer: { text: "text-yellow-400", bg: "bg-yellow-900/10", border: "border-yellow-900/30", dot: "bg-yellow-400" },
};

/**
 * Extract readable text from a raw token stream.
 *
 * The stream contains two types of content:
 * 1. <think>...</think> blocks — natural language reasoning (local models)
 * 2. JSON output — structured artifact being generated
 *
 * We extract the <think> reasoning verbatim, and parse the JSON to
 * pull out human-readable fields (summary, rationale, titles, etc.)
 * instead of showing raw JSON characters.
 */
function extractReadableContent(raw: string): { reasoning: string; fields: ParsedField[] } {
  let reasoning = "";
  let jsonPart = raw;

  // Extract <think> block content (may be incomplete/unclosed during streaming)
  const thinkOpen = raw.indexOf("<think>");
  if (thinkOpen !== -1) {
    const thinkClose = raw.indexOf("</think>");
    if (thinkClose !== -1) {
      // Complete <think> block
      reasoning = raw.substring(thinkOpen + 7, thinkClose).trim();
      jsonPart = raw.substring(thinkClose + 8).trim();
    } else {
      // Still streaming inside <think> — show what we have so far
      reasoning = raw.substring(thinkOpen + 7).trim();
      return { reasoning, fields: [] };
    }
  }

  // Try to extract readable fields from the JSON portion
  const fields = extractFieldsFromPartialJson(jsonPart);

  return { reasoning, fields };
}

/**
 * Extract human-readable key-value pairs from potentially incomplete JSON.
 *
 * Rather than trying to parse incomplete JSON, we use regex to find
 * completed "key": "value" pairs for known interesting fields.
 */
function extractFieldsFromPartialJson(json: string): ParsedField[] {
  const fields: ParsedField[] = [];
  if (!json || json.length < 5) return fields;

  // Fields we care about, in display order
  const fieldDefs: { key: string; label: string }[] = [
    { key: "summary", label: "Summary" },
    { key: "rationale", label: "Rationale" },
    { key: "title", label: "Task" },
    { key: "description", label: "Description" },
    { key: "details", label: "Details" },
    { key: "acceptance", label: "Acceptance" },
    { key: "recommended_fix", label: "Fix" },
  ];

  for (const { key, label } of fieldDefs) {
    // Match "key": "value" — handle escaped quotes inside value
    const pattern = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)(?:"|$)`, "g");
    let match;
    while ((match = pattern.exec(json)) !== null) {
      const value = match[1]
        .replace(/\\n/g, "\n")
        .replace(/\\"/g, '"')
        .replace(/\\\\/g, "\\")
        .trim();
      if (value.length > 0) {
        // For "title" fields, prefix with a bullet if we already have one
        const existingTitles = fields.filter((f) => f.label === label);
        const displayLabel = existingTitles.length > 0 ? label : label;
        fields.push({ label: displayLabel, value });
      }
    }
  }

  // Also extract severity from review issues
  const severityPattern = /"severity"\s*:\s*"(\w+)"/g;
  const titlePattern = /"title"\s*:\s*"((?:[^"\\]|\\.)*)"/g;
  const severities: string[] = [];
  const titles: string[] = [];
  let m;
  while ((m = severityPattern.exec(json)) !== null) severities.push(m[1]);
  while ((m = titlePattern.exec(json)) !== null) {
    titles.push(m[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\"));
  }

  // If we found severity+title pairs that aren't already in fields as "Task",
  // and they look like review issues, add them
  if (severities.length > 0 && severities.length === titles.length) {
    const existingTitles = new Set(fields.filter((f) => f.label === "Task").map((f) => f.value));
    for (let i = 0; i < severities.length; i++) {
      if (!existingTitles.has(titles[i]) && severities[i] !== "info") {
        fields.push({
          label: `Issue (${severities[i]})`,
          value: titles[i],
        });
      }
    }
  }

  return fields;
}

/**
 * Live Thinking — streams model reasoning in real-time during active runs.
 *
 * Separates <think> reasoning (natural language) from JSON output,
 * and extracts readable fields from the structured output as they
 * stream in. Shows clean, formatted content instead of raw JSON.
 */
export default function LiveThinking({ events, isActive }: Props) {
  const [buffers, setBuffers] = useState<Record<string, RoleBuffer>>({});
  const processedRef = useRef(0);
  const scrollRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Process new events incrementally
  useEffect(() => {
    if (processedRef.current >= events.length) return;

    const newEvents = events.slice(processedRef.current);
    processedRef.current = events.length;

    setBuffers((prev) => {
      const next = { ...prev };

      for (const evt of newEvents) {
        // Token chunk — accumulate into buffer
        if (evt.event === "token" && typeof evt.text === "string" && typeof evt.role === "string") {
          const role = evt.role;
          if (!next[role]) {
            next[role] = { raw: "", reasoning: "", fields: [], active: true, collapsed: false };
          } else {
            next[role] = { ...next[role] };
          }
          next[role].raw += evt.text;
          // Cap raw buffer at 100KB to prevent memory issues
          if (next[role].raw.length > 100_000) {
            next[role].raw = next[role].raw.slice(-50_000);
          }
          next[role].active = true;

          // Re-extract readable content from updated raw buffer
          const { reasoning, fields } = extractReadableContent(next[role].raw);
          next[role].reasoning = reasoning;
          next[role].fields = fields;
        }

        // Role start — clear buffer for new stage
        if (evt.event === "architect_start" || evt.event === "implementer_start" || evt.event === "reviewer_start") {
          const role = evt.event.replace("_start", "");
          next[role] = { raw: "", reasoning: "", fields: [], active: true, collapsed: false };
        }

        // Role complete — auto-collapse
        if (
          evt.event === "architect_complete" ||
          evt.event === "implementer_complete" ||
          evt.event === "reviewer_complete" ||
          evt.event === "architect_reasoning" ||
          evt.event === "implementer_reasoning" ||
          evt.event === "reviewer_reasoning"
        ) {
          const role = evt.event.split("_")[0];
          if (next[role]) {
            next[role] = { ...next[role], active: false, collapsed: true };
          }
        }

        // Pipeline complete — collapse everything
        if (evt.event === "pipeline_complete" || evt.event === "error" || evt.event === "terminated") {
          for (const role of Object.keys(next)) {
            next[role] = { ...next[role], active: false, collapsed: true };
          }
        }
      }

      return next;
    });
  }, [events]);

  // Auto-scroll active buffers
  useEffect(() => {
    for (const [role, buf] of Object.entries(buffers)) {
      if (buf.active && !buf.collapsed && scrollRefs.current[role]) {
        const el = scrollRefs.current[role];
        if (el) {
          el.scrollTop = el.scrollHeight;
        }
      }
    }
  }, [buffers]);

  // Don't render if no tokens have been received
  const hasContent = Object.values(buffers).some((b) => b.raw.length > 0);
  if (!hasContent) return null;

  const roles = Object.entries(buffers).filter(
    ([, b]) => b.raw.length > 0
  );

  return (
    <div className="mb-6">
      <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">
        Live Thinking
      </h2>
      <div className="space-y-2">
        {roles.map(([role, buf]) => {
          const colors = ROLE_COLORS[role] || ROLE_COLORS.architect;
          const label = ROLE_LABELS[role] || role;
          const hasReasoning = buf.reasoning.length > 0;
          const hasFields = buf.fields.length > 0;

          return (
            <div
              key={role}
              className={`${colors.bg} border ${colors.border} rounded-lg overflow-hidden`}
            >
              {/* Header */}
              <div
                className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-white/5 transition-colors"
                onClick={() => {
                  setBuffers((prev) => ({
                    ...prev,
                    [role]: { ...prev[role], collapsed: !prev[role].collapsed },
                  }));
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setBuffers((prev) => ({
                      ...prev,
                      [role]: { ...prev[role], collapsed: !prev[role].collapsed },
                    }));
                  }
                }}
                role="button"
                tabIndex={0}
                aria-expanded={!buf.collapsed}
                title={buf.collapsed ? `Expand ${label} reasoning` : `Collapse ${label} reasoning`}
              >
                {buf.active ? (
                  <span className={`w-2 h-2 rounded-full ${colors.dot} animate-pulse`} />
                ) : (
                  <span className="w-2 h-2 rounded-full bg-gray-600" />
                )}

                <span className={`text-xs font-medium ${colors.text}`}>
                  {label}
                </span>

                {buf.active ? (
                  <span className="text-[10px] text-gray-500 animate-pulse">thinking...</span>
                ) : (
                  <span className="text-[10px] text-gray-600">done</span>
                )}

                {/* Preview of first field value in collapsed state */}
                {buf.collapsed && hasFields && (
                  <span className="text-[10px] text-gray-600 truncate max-w-[300px] ml-1">
                    — {buf.fields[0].value.slice(0, 80)}{buf.fields[0].value.length > 80 ? "..." : ""}
                  </span>
                )}

                <span className="text-gray-600 text-xs ml-auto shrink-0">
                  {buf.collapsed ? "\u25B6" : "\u25BC"}
                </span>
              </div>

              {/* Content body */}
              {!buf.collapsed && (
                <div
                  ref={(el) => { scrollRefs.current[role] = el; }}
                  className="px-3 pb-3 pt-0 overflow-y-auto max-h-[300px]"
                >
                  {/* Chain-of-thought reasoning (from <think> blocks) */}
                  {hasReasoning && (
                    <div className="mb-2">
                      <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Reasoning</p>
                      <p className="text-[11px] leading-relaxed text-gray-300 whitespace-pre-wrap">
                        {buf.reasoning}
                        {buf.active && !hasFields && (
                          <span className="inline-block w-1.5 h-3.5 bg-gray-400 animate-pulse ml-0.5 align-text-bottom" />
                        )}
                      </p>
                    </div>
                  )}

                  {/* Extracted structured fields */}
                  {hasFields && (
                    <div className="space-y-1.5">
                      {buf.fields.map((field, i) => (
                        <div key={`${field.label}-${i}`}>
                          <span className="text-[10px] text-gray-500 uppercase tracking-wider">{field.label}</span>
                          <p className="text-[11px] leading-relaxed text-gray-300 whitespace-pre-wrap mt-0.5">
                            {field.value}
                            {buf.active && i === buf.fields.length - 1 && (
                              <span className="inline-block w-1.5 h-3.5 bg-gray-400 animate-pulse ml-0.5 align-text-bottom" />
                            )}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Fallback: activity indicator when no reasoning or fields parsed */}
                  {!hasReasoning && !hasFields && buf.raw.length > 0 && (
                    <div className="flex items-center gap-2">
                      <p className="text-[11px] text-gray-400">
                        Generating code{buf.active ? "..." : ""}
                        <span className="text-gray-600 ml-2">
                          {(buf.raw.length / 1024).toFixed(1)}KB output
                        </span>
                      </p>
                      {buf.active && (
                        <span className="inline-block w-1.5 h-3.5 bg-gray-400 animate-pulse" />
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
  );
}
