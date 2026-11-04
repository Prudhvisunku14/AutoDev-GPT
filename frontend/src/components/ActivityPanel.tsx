import { Activity, ArrowUpRight } from "lucide-react";
import { Link } from "react-router";
import { cn } from "../lib/cn";
import { formatDuration } from "../lib/format";
import { deriveStages, STATUS_WORDS, type Stage } from "../lib/stages";
import type { Run, RunEvent } from "../lib/types";
import { StageIcon } from "./StageIcon";

function overall(run: Run | null, active: boolean): { label: string; tone: string } {
  if (!run) return { label: "Idle", tone: "bg-slate-100 text-slate-600" };
  if (run.state === "COMPLETED") return { label: "Completed", tone: "bg-emerald-50 text-emerald-700" };
  if (run.state === "FAILED" || run.state === "STOPPED_BY_POLICY") return { label: run.state === "FAILED" ? "Failed" : "Stopped", tone: "bg-rose-50 text-rose-700" };
  if (run.state === "CANCELLED") return { label: "Cancelled", tone: "bg-slate-100 text-slate-600" };
  if (run.state === "AWAITING_APPROVAL") return { label: "Waiting", tone: "bg-amber-50 text-amber-700" };
  if (run.pause_reason) return { label: "Paused", tone: "bg-amber-50 text-amber-700" };
  if (run.state === "CREATED") return { label: "Not started", tone: "bg-slate-100 text-slate-600" };
  return active ? { label: "Running", tone: "bg-emerald-50 text-emerald-700" } : { label: "Idle", tone: "bg-slate-100 text-slate-600" };
}

/** What the stage's corner shows: its duration once done, nothing while pending, otherwise its status word. */
function cornerText(stage: Stage): string {
  if (stage.status === "completed" && stage.elapsedMs !== null) return formatDuration(stage.elapsedMs);
  return stage.status === "pending" ? "" : STATUS_WORDS[stage.status];
}

/** The five stages of one run, from its real state and events. With no run, every stage is pending. */
export function ActivityPanel({ run, events, projectName, className }: { run: Run | null; events: RunEvent[]; projectName?: string; className?: string }) {
  const view = deriveStages(run, events);
  const badge = overall(run, view.active);
  const percent = Math.round((view.finished / view.total) * 100);
  const recoveries = events.filter((event) => event.type === "recovery.decided");
  const latestRecovery = recoveries.at(-1)?.payload;
  const failure = [...events].reverse().find((event) => ["llm.error", "run.failed", "run.stopped"].includes(event.type));
  const generatedFiles = [...new Set(events
    .filter((event) => event.type === "coding.finished" && event.payload.kept === true)
    .flatMap((event) => (Array.isArray(event.payload.changes) ? event.payload.changes : [])
      .flatMap((change) => change && typeof change === "object" && "path" in change && typeof change.path === "string" ? [change.path] : [])))];
  const test = [...events].reverse().find((event) => event.type === "verification.finished");
  const verification = [...events].reverse().find((event) => event.type === "acceptance.finished");

  return (
    <section aria-labelledby="live-activity" className={cn("rounded-2xl border border-line bg-white/90 p-5 shadow-soft backdrop-blur", className)}>
      <header className="flex items-center justify-between gap-3">
        <h2 id="live-activity" className="flex items-center gap-2 text-[15px] font-semibold">
          <Activity className="size-4 text-violet-600" aria-hidden />
          Live Activity
        </h2>
        <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium", badge.tone)} data-testid="activity-status">
          {badge.label === "Running" && <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" aria-hidden />}
          {badge.label}
        </span>
      </header>

      {run ? (
        <p className="mt-1.5 truncate text-xs text-muted">
          {projectName ? `${projectName} · ` : ""}
          <Link to={`/runs/${run.id}`} className="inline-flex items-center gap-0.5 text-violet-700 hover:underline">
            View run <ArrowUpRight className="size-3" aria-hidden />
          </Link>
        </p>
      ) : (
        <p className="mt-1.5 text-xs text-muted">No run yet. Stages appear here as a build reports them.</p>
      )}

      <ol className="mt-5 space-y-0">
        {view.stages.map((stage, index) => (
          <li key={stage.key} className="relative flex gap-3 pb-5 last:pb-0" data-testid={`stage-${stage.key}`} data-status={stage.status}>
            {index < view.stages.length - 1 && (
              <span className={cn("absolute top-6 left-[9.5px] h-[calc(100%-1.25rem)] w-px", stage.status === "completed" ? "bg-emerald-200" : "bg-line")} aria-hidden />
            )}
            <StageIcon status={stage.status} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <p className="text-sm font-semibold">{stage.label}</p>
                <span className="shrink-0 text-xs text-muted">
                  {cornerText(stage) !== STATUS_WORDS[stage.status] && <span className="sr-only">{STATUS_WORDS[stage.status]} </span>}
                  {cornerText(stage)}
                </span>
              </div>
              <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-muted" title={stage.summary}>
                {stage.summary}
              </p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-5">
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="font-medium">Progress</span>
          <span className="text-muted" data-testid="activity-progress">
            {run ? `${view.finished} of ${view.total} stages` : "—"}
          </span>
        </div>
        <div
          className="relative h-1.5 overflow-hidden rounded-full bg-violet-100/70"
          role="progressbar"
          aria-label="Stages finished"
          aria-valuemin={0}
          aria-valuemax={view.total}
          aria-valuenow={run && !(view.active && view.finished === 0) ? view.finished : undefined}
        >
          {view.active && view.finished === 0 ? (
            <span className="absolute inset-y-0 w-1/3 animate-shimmer rounded-full bg-gradient-to-r from-indigo-400 to-violet-500" />
          ) : (
            <span className="block h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500 transition-[width] duration-500" style={{ width: `${run ? percent : 0}%` }} />
          )}
        </div>
      </div>

      {!!generatedFiles.length && (
        <div className="mt-4 rounded-xl bg-slate-50 p-3" aria-label="Generated files">
          <p className="text-xs font-semibold">Generated files</p>
          <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
            {generatedFiles.slice(0, 8).map((path) => <li key={path} className="font-mono">{path}</li>)}
            {generatedFiles.length > 8 && <li>+{generatedFiles.length - 8} more</li>}
          </ul>
        </div>
      )}
      {(test || verification) && (
        <div className="mt-3 space-y-1 text-xs text-muted" aria-label="Test results">
          {test && <p>Sandbox tests: {String(test.payload.verdict)} · {String(test.payload.passed ?? 0)} of {String(test.payload.ran ?? 0)} passed</p>}
          {verification && <p>Independent verification: {String(verification.payload.verdict)} · {String(verification.payload.passed ?? 0)} of {String(verification.payload.total ?? 0)} criteria</p>}
        </div>
      )}
      {!!recoveries.length && (
        <p className="mt-3 text-xs text-amber-700" aria-label="Recovery attempts">
          {recoveries.length} recovery attempt{recoveries.length === 1 ? "" : "s"} recorded
          {latestRecovery ? ` · latest action: ${String(latestRecovery.action ?? "recorded")}` : ""}
        </p>
      )}
      {failure && (
        <p className="mt-3 text-xs text-rose-700" role="status">
          Latest error: {String(failure.payload.message ?? failure.payload.detail ?? failure.payload.reason ?? failure.type)}
        </p>
      )}
    </section>
  );
}
