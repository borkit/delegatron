"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getOllamaStatus, listRuns, type OllamaStatus, type RunSummary } from "@/lib/api";

const NAV_ITEMS = [
  { href: "/", label: "Dashboard", icon: "H", title: "Home dashboard" },
  { href: "/runs", label: "Runs", icon: "R", title: "Run history" },
  { href: "/review", label: "Review", icon: "Q", title: "Code review and log analysis — read-only" },
  { href: "/compliance", label: "Compliance", icon: "C", title: "Zero-cloud verification" },
  { href: "/templates", label: "Templates", icon: "T", title: "Run templates" },
  { href: "/analytics", label: "Analytics", icon: "A", title: "Usage analytics" },
  { href: "/compare", label: "Compare", icon: "V", title: "Compare two runs" },
  { href: "/projects", label: "Projects", icon: "P", title: "Multi-milestone project mode (validated 2026-04-17)" },
  { href: "/settings", label: "Settings", icon: "S", title: "Configuration" },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [activeRuns, setActiveRuns] = useState<RunSummary[]>([]);

  useEffect(() => {
    getOllamaStatus().then(setOllama).catch(() => {});

    // Poll for active runs every 3 seconds
    function checkActive() {
      listRuns()
        .then((runs) => {
          setActiveRuns(runs.filter((r) =>
            r.status === "running" || r.status === "started" || r.status === "in-progress"
          ));
        })
        .catch(() => {});
    }
    checkActive();
    const interval = setInterval(checkActive, 3000);
    return () => clearInterval(interval);
  }, []);

  return (
    <aside className="w-56 min-h-screen bg-gray-900 border-r border-gray-800 flex flex-col">
      <div className="p-4 border-b border-gray-800">
        <h1 className="text-lg font-bold text-white tracking-tight">
          Delegatron
        </h1>
        <p className="text-xs text-gray-500 mt-0.5">Pipeline Dashboard</p>
      </div>
      <div className="p-3 pb-0">
        <Link
          href="/new"
          title="Start a new pipeline run from a natural language request"
          className={`flex items-center justify-center gap-2 w-full px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
            pathname === "/new"
              ? "bg-blue-600 text-white"
              : "bg-blue-600/20 text-blue-400 hover:bg-blue-600/30"
          }`}
        >
          <span className="text-lg leading-none">+</span>
          New Run
        </Link>
      </div>
      <nav className="flex-1 p-3 space-y-1">
        {NAV_ITEMS.map((item) => {
          const active =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              title={item.title}
              className={`flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
                active
                  ? "bg-blue-600/20 text-blue-400"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              }`}
            >
              <span className="w-6 h-6 flex items-center justify-center rounded bg-gray-800 text-xs font-mono font-bold" aria-hidden="true">
                {item.icon}
              </span>
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* Active runs indicator */}
      {activeRuns.length > 0 && (
        <div className="px-3 pb-2">
          <Link
            href={`/runs/${activeRuns[0].run_id}`}
            title="View the currently running pipeline"
            className="block bg-blue-900/30 border border-blue-800 rounded-lg px-3 py-2.5 hover:bg-blue-900/40 transition-colors"
          >
            <div className="flex items-center gap-2 mb-1">
              <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
              <span className="text-xs text-blue-400 font-medium">
                {activeRuns.length === 1 ? "Run in progress" : `${activeRuns.length} runs active`}
              </span>
            </div>
            <p className="text-[10px] text-blue-300/70 truncate">
              {activeRuns[0].request?.slice(0, 50) || activeRuns[0].run_id.slice(0, 8)}
            </p>
          </Link>
        </div>
      )}

      {/* Ollama status */}
      <div className="px-4 py-2">
        {ollama === null ? (
          <div className="text-xs text-gray-600">Checking Ollama...</div>
        ) : ollama.running ? (
          <div className="bg-green-900/20 border border-green-900/30 rounded-lg px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-green-500" />
              <span className="text-xs text-green-400 font-medium">Ollama</span>
            </div>
            {ollama.models.length > 0 && (
              <p className="text-[10px] text-green-600 mt-1 truncate" title={ollama.models.join(", ")}>
                {ollama.models.length} model{ollama.models.length !== 1 ? "s" : ""}
              </p>
            )}
          </div>
        ) : (
          <div className="bg-gray-800/50 border border-gray-800 rounded-lg px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-gray-600" />
              <span className="text-xs text-gray-500">Ollama offline</span>
            </div>
          </div>
        )}
      </div>

      <div className="p-4 border-t border-gray-800">
        <span className="text-xs text-gray-600">StoriaBrevi LLC</span>
      </div>
    </aside>
  );
}
