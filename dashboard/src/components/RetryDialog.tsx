"use client";

import { useEffect, useRef, useState } from "react";

interface RunMeta {
  preset?: string;
  sandbox?: string;
  docker_image?: string;
  max_iterations?: number;
  max_cost_usd?: number;
  architect?: { model?: string };
  implementer?: { model?: string };
  reviewer?: { model?: string };
}

interface RetryDialogProps {
  open: boolean;
  meta: RunMeta | null;
  onConfirm: (opts: { freshStart: boolean; preset?: string }) => void;
  onCancel: () => void;
}

const PRESET_GROUPS = [
  {
    label: "Local / Ollama",
    options: [
      { value: "local-oss", label: "Local OSS ★ — phi4 + gpt-oss:20b (best local quality)" },
      { value: "local", label: "Local (default) — phi4 + qwen2.5-coder:14b (fits 12 GB)" },
      { value: "local-oss-review", label: "Local OSS Review — qwen2.5-coder + gpt-oss reviewer" },
      { value: "local-phi4", label: "Local Phi-4 — phi4:14b all (fits 12 GB, fastest)" },
      { value: "local-heavy", label: "Local Heavy ⚠️ — qwen3-coder:30b (requires 24 GB+ VRAM)" },
    ],
  },
  {
    label: "Cloud",
    options: [
      { value: "default", label: "Cloud Trio — Claude + GPT + Gemini" },
      { value: "claude-heavy", label: "Claude Heavy — Claude arch + impl" },
      { value: "budget", label: "Budget — all Gemini Flash" },
      { value: "single-claude", label: "Single Claude — all Sonnet" },
      { value: "single-gpt", label: "Single GPT — all GPT-4.1" },
    ],
  },
  {
    label: "Testing",
    options: [
      { value: "mock", label: "Mock — no API keys" },
    ],
  },
];

export default function RetryDialog({ open, meta, onConfirm, onCancel }: RetryDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [preset, setPreset] = useState("");
  const [freshStart, setFreshStart] = useState(false);

  // Reset state when dialog opens
  useEffect(() => {
    if (open) {
      setPreset("");
      setFreshStart(false);
      cancelRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [open, onCancel]);

  if (!open) return null;

  const origPreset = meta?.preset || "unknown";
  const origArch = meta?.architect?.model;
  const origImpl = meta?.implementer?.model;
  const origRev = meta?.reviewer?.model;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onCancel} />
      <div className="relative bg-gray-900 border border-gray-700 rounded-xl shadow-2xl max-w-md w-full mx-4 p-6">
        <h3 className="text-lg font-semibold text-white">Retry Run</h3>
        <p className="text-sm text-gray-400 mt-1">
          Adjust parameters or keep the original config.
        </p>

        {/* Original config summary */}
        <div className="mt-4 p-3 bg-gray-800/60 rounded-lg text-xs text-gray-400 space-y-1">
          <div className="text-gray-500 font-medium mb-1">Original Config</div>
          <div>Preset: <span className="text-gray-300">{origPreset}</span></div>
          {origArch && <div>Architect: <span className="text-gray-300">{origArch}</span></div>}
          {origImpl && <div>Implementer: <span className="text-gray-300">{origImpl}</span></div>}
          {origRev && <div>Reviewer: <span className="text-gray-300">{origRev}</span></div>}
          {meta?.sandbox && <div>Sandbox: <span className="text-gray-300">{meta.sandbox}</span></div>}
          {meta?.max_iterations && <div>Max iterations: <span className="text-gray-300">{meta.max_iterations}</span></div>}
        </div>

        {/* Preset override */}
        <div className="mt-4">
          <label className="block text-xs font-medium text-gray-400 mb-1.5">Preset</label>
          <select
            value={preset}
            onChange={(e) => setPreset(e.target.value)}
            className="w-full px-3 py-2 text-sm bg-gray-800 text-gray-200 border border-gray-700 rounded-lg focus:outline-none focus:border-blue-600"
          >
            <option value="">Keep original ({origPreset})</option>
            {PRESET_GROUPS.map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.options.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>

        {/* Fresh start toggle */}
        <label className="flex items-center gap-2 mt-4 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={freshStart}
            onChange={(e) => setFreshStart(e.target.checked)}
            className="rounded border-gray-700 bg-gray-800 text-blue-500 h-4 w-4"
          />
          <div>
            <span className="text-sm text-gray-300">Fresh start</span>
            <p className="text-xs text-gray-500">Ignore previous review issues and start from scratch</p>
          </div>
        </label>

        {/* Actions */}
        <div className="flex justify-end gap-3 mt-6">
          <button
            ref={cancelRef}
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium text-gray-300 bg-gray-800 border border-gray-700 rounded-lg hover:bg-gray-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm({ freshStart, preset: preset || undefined })}
            className="px-4 py-2 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    </div>
  );
}
