import { cn } from "../lib/cn";
import { stateLabel } from "../lib/format";

const TONES: Record<string, string> = {
  COMPLETED: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  FAILED: "bg-rose-50 text-rose-700 ring-rose-200",
  STOPPED_BY_POLICY: "bg-rose-50 text-rose-700 ring-rose-200",
  CANCELLED: "bg-slate-100 text-slate-600 ring-slate-200",
  AWAITING_APPROVAL: "bg-amber-50 text-amber-700 ring-amber-200",
  CREATED: "bg-slate-50 text-slate-600 ring-slate-200",
};
const WORKING = "bg-violet-50 text-violet-700 ring-violet-200";

/** A run state as a small pill. A paused run says so: it is neither working nor finished. */
export function StatusBadge({ state, paused, className }: { state: string; paused?: boolean; className?: string }) {
  const tone = paused ? "bg-amber-50 text-amber-700 ring-amber-200" : (TONES[state] ?? WORKING);
  const label = paused ? `Paused · ${stateLabel(state)}` : state === "STOPPED_BY_POLICY" ? "Stopped" : stateLabel(state);
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset", tone, className)}>
      {!paused && !TONES[state] && <span className="size-1.5 animate-pulse rounded-full bg-violet-500" aria-hidden />}
      {label}
    </span>
  );
}
