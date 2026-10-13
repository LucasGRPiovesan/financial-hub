import { cn } from "../cn";
import type { StreamStatus } from "./use-project-stream";

const LABELS: Record<StreamStatus, string> = {
  connecting: "Connecting",
  open: "Live",
  retrying: "Reconnecting",
  closed: "Offline",
};

const DOT: Record<StreamStatus, string> = {
  connecting: "bg-yellow-500",
  open: "bg-green-500",
  retrying: "bg-yellow-500 animate-pulse",
  closed: "bg-red-500",
};

/** Small realtime connection indicator. */
export function StreamStatusIndicator({ status, className }: { status: StreamStatus; className?: string }) {
  return (
    <span role="status" className={cn("inline-flex items-center gap-1.5 text-xs", className)}>
      <span aria-hidden className={cn("size-2 rounded-full", DOT[status])} />
      {LABELS[status]}
    </span>
  );
}
