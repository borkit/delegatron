interface StatusBadgeProps {
  status: string;
}

const STATUS_STYLES: Record<string, string> = {
  approved: "bg-green-900/50 text-green-400 border-green-800",
  completed: "bg-green-900/50 text-green-400 border-green-800",
  running: "bg-blue-900/50 text-blue-400 border-blue-800",
  started: "bg-blue-900/50 text-blue-400 border-blue-800",
  "in-progress": "bg-yellow-900/50 text-yellow-400 border-yellow-800",
  exhausted: "bg-orange-900/50 text-orange-400 border-orange-800",
  stalled: "bg-orange-900/50 text-orange-400 border-orange-800",
  partial: "bg-yellow-900/50 text-yellow-400 border-yellow-800",
  failed: "bg-red-900/50 text-red-400 border-red-800",
  error: "bg-red-900/50 text-red-400 border-red-800",
  cancelled: "bg-gray-800 text-gray-400 border-gray-700",
};

// Map internal pipeline statuses to user-friendly labels
const STATUS_LABELS: Record<string, string> = {
  approved: "completed",
  exhausted: "failed (max retries)",
  stalled: "failed (stalled)",
  partial: "partially completed",
  "in-progress": "running",
  started: "running",
};

const STATUS_TOOLTIPS: Record<string, string> = {
  approved: "Pipeline completed — reviewer accepted and all quality gates passed",
  completed: "Pipeline completed successfully",
  running: "Pipeline is actively running",
  started: "Pipeline has been started and is initializing",
  "in-progress": "Pipeline is in progress",
  exhausted: "Pipeline hit the maximum iteration limit without getting reviewer approval",
  stalled: "Pipeline detected a stuck pattern — same issues repeating across iterations",
  partial: "Pipeline finished — some tasks were approved, others were skipped or not completed",
  failed: "Pipeline encountered an error and stopped",
  error: "Pipeline encountered an error and stopped",
  cancelled: "Pipeline was manually cancelled by the user",
};

const DEFAULT_STYLE = "bg-gray-800 text-gray-400 border-gray-700";

export default function StatusBadge({ status }: StatusBadgeProps) {
  const style = STATUS_STYLES[status] || DEFAULT_STYLE;
  const label = STATUS_LABELS[status] || status;
  const tooltip = STATUS_TOOLTIPS[status] || status;
  return (
    <span
      role="status"
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${style}`}
      title={tooltip}
    >
      {label}
    </span>
  );
}
