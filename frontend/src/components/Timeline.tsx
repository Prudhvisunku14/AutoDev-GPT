import { Bot, CheckCircle2, FileCode2, FlaskConical, Info, RefreshCw, TriangleAlert, Wrench, XCircle } from "lucide-react";
import { useState } from "react";
import { cn } from "../lib/cn";
import { formatDateTime } from "../lib/format";
import type { TimelineItem, TimelineKind } from "../lib/timeline";

const ICONS: Record<TimelineKind, typeof Info> = {
  state: RefreshCw,
  gpt: Bot,
  tool: Wrench,
  coding: FileCode2,
  files: FileCode2,
  test: FlaskConical,
  recovery: RefreshCw,
  error: TriangleAlert,
  delivery: CheckCircle2,
  milestone: CheckCircle2,
  info: Info,
};
const TONES = { neutral: "text-slate-500 bg-slate-100", good: "text-emerald-600 bg-emerald-50", bad: "text-rose-600 bg-rose-50", warn: "text-amber-600 bg-amber-50" };
const INITIAL = 60;

/** The grouped history of one run. Long histories show their latest part first, with a way to see the rest. */
export function Timeline({ items }: { items: TimelineItem[] }) {
  const [all, setAll] = useState(false);
  if (!items.length) return <p className="text-sm text-muted">Nothing recorded yet.</p>;
  const shown = all || items.length <= INITIAL ? items : items.slice(-INITIAL);
  return (
    <div>
      {!all && items.length > INITIAL && (
        <button type="button" onClick={() => setAll(true)} className="mb-3 text-xs font-medium text-violet-700 hover:underline">
          Show {items.length - INITIAL} earlier entries
        </button>
      )}
      <ol className="space-y-3" aria-label="Run activity">
        {shown.map((item) => {
          const Icon = item.tone === "bad" && item.kind !== "error" ? XCircle : ICONS[item.kind];
          return (
            <li key={item.key} className="flex gap-3">
              <span className={cn("mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg", TONES[item.tone])}>
                <Icon className="size-3.5" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium break-words">{item.title}</p>
                {item.detail && <p className="mt-0.5 text-xs leading-relaxed break-words text-muted">{item.detail}</p>}
              </div>
              <time className="shrink-0 pt-0.5 text-[11px] text-slate-400" dateTime={item.ts}>
                {formatDateTime(item.ts)}
              </time>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
