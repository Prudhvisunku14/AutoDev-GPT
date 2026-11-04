import { CheckCircle2, Loader2, Settings2 } from "lucide-react";
import { cn } from "../lib/cn";
import type { Readiness } from "../lib/readiness";

/** One short line: whether a build can start. The diagnostics behind it are folded away, for whoever needs them. */
export function BuildStatus({ readiness }: { readiness: Readiness }) {
  const Icon = readiness.kind === "ready" ? CheckCircle2 : readiness.kind === "checking" ? Loader2 : Settings2;
  return (
    <div className="px-1 text-sm" data-testid="build-status" data-kind={readiness.kind}>
      <p role="status" className={cn("inline-flex items-center gap-2 font-medium", readiness.kind === "ready" ? "text-emerald-700" : readiness.kind === "checking" ? "text-muted" : "text-amber-800")}>
        <Icon className={cn("size-4 shrink-0", readiness.kind === "checking" && "animate-spin")} aria-hidden />
        {readiness.summary}
      </p>
      {readiness.details.length > 0 && (
        <details className="mt-1 text-xs text-muted">
          <summary className="cursor-pointer select-none">Advanced details</summary>
          <ul className="mt-1 list-disc space-y-1 pl-4 [overflow-wrap:anywhere]">
            {readiness.details.map((detail) => <li key={detail}>{detail}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}
