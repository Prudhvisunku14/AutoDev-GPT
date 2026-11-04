import { Ban, CheckCircle2, Circle, Hourglass, Loader2, MinusCircle, PauseCircle, XCircle } from "lucide-react";
import { cn } from "../lib/cn";
import type { StageStatus } from "../lib/stages";

const ICONS = {
  pending: { icon: Circle, className: "text-slate-300" },
  running: { icon: Loader2, className: "animate-spin text-violet-600" },
  waiting: { icon: Hourglass, className: "text-amber-500" },
  paused: { icon: PauseCircle, className: "text-amber-500" },
  completed: { icon: CheckCircle2, className: "text-emerald-500" },
  skipped: { icon: MinusCircle, className: "text-slate-400" },
  blocked: { icon: Ban, className: "text-rose-500" },
  failed: { icon: XCircle, className: "text-rose-500" },
  cancelled: { icon: XCircle, className: "text-slate-400" },
} satisfies Record<StageStatus, { icon: typeof Circle; className: string }>;

export function StageIcon({ status, className }: { status: StageStatus; className?: string }) {
  const { icon: Icon, className: tone } = ICONS[status];
  return <Icon className={cn("size-5 shrink-0", tone, className)} aria-hidden />;
}
