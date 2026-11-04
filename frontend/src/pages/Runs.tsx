import { ChevronRight, Loader2, PlayCircle } from "lucide-react";
import { Link } from "react-router";
import { EmptyState } from "../components/EmptyState";
import { Notice } from "../components/Notice";
import { PageTitle } from "../components/PageTitle";
import { StatusBadge } from "../components/StatusBadge";
import { testResult } from "../lib/delivery";
import { formatCost, formatDateTime, shortId } from "../lib/format";
import { useAllRuns, useReports } from "../lib/queries";
import type { Report } from "../lib/types";
import { HistoricalRuns } from "../components/HistoricalRuns";

function provider(report: Report | null | undefined): string {
  if (!report) return "—";
  const providers = report.provenance?.coding_providers ?? [];
  return providers.length ? providers.join(", ") : "none used";
}

export function Runs() {
  const { entries, isLoading, error } = useAllRuns();
  const reports = useReports(entries.map((e) => e.run));

  return (
    <div className="mx-auto max-w-6xl pt-8">
      <PageTitle title="Runs" subtitle="Every build on this backend, newest first. Provider and tests come from a finished run's report." />
      {error ? <Notice tone="error" title="Runs could not be loaded" className="mb-4">{error instanceof Error ? error.message : "Unknown error"}</Notice> : null}
      {isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden /> Loading runs…
        </p>
      ) : entries.length ? (
        <div className="overflow-hidden rounded-2xl border border-line bg-white shadow-soft">
          {/* relative: the header's sr-only label is absolutely positioned, and without a positioned scroll container it
              escapes the clipping and widens the whole page on a phone. */}
          <div className="relative overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="border-b border-line bg-slate-50/70 text-xs font-medium tracking-wide text-muted uppercase">
                <tr>
                  <th scope="col" className="px-5 py-3">Project</th>
                  <th scope="col" className="px-3 py-3">Run</th>
                  <th scope="col" className="px-3 py-3">State</th>
                  <th scope="col" className="px-3 py-3">Started</th>
                  <th scope="col" className="px-3 py-3">Provider</th>
                  <th scope="col" className="px-3 py-3">Cost</th>
                  <th scope="col" className="px-3 py-3">Tests</th>
                  <th scope="col" className="px-3 py-3">Outcome</th>
                  <th scope="col" className="px-3 py-3"><span className="sr-only">Open</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {entries.map(({ project, run }) => {
                  const report = reports.get(run.id);
                  return (
                    <tr key={run.id} className="transition hover:bg-violet-50/40">
                      <td className="max-w-[16rem] truncate px-5 py-3.5 font-medium">{project.name}</td>
                      <td className="px-3 py-3.5 font-mono text-xs text-muted">{shortId(run.id)}</td>
                      <td className="px-3 py-3.5">
                        <StatusBadge state={run.state} paused={!!run.pause_reason} />
                      </td>
                      <td className="px-3 py-3.5 text-muted">{formatDateTime(run.started_at)}</td>
                      <td className="px-3 py-3.5 text-muted">{provider(report)}</td>
                      <td className="px-3 py-3.5 text-muted">{formatCost(run)}</td>
                      <td className="px-3 py-3.5 text-muted">{testResult(report ?? null)}</td>
                      <td className="max-w-[12rem] truncate px-3 py-3.5 text-muted" title={run.status_detail ?? undefined}>
                        {run.outcome ?? "—"}
                      </td>
                      <td className="px-3 py-3.5">
                        <Link to={`/runs/${run.id}`} className="inline-flex items-center gap-0.5 font-medium text-violet-700 hover:underline" aria-label={`Open run ${shortId(run.id)} of ${project.name}`}>
                          Open <ChevronRight className="size-4" aria-hidden />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : <EmptyState icon={PlayCircle} title="No runs yet">A run appears here when a build starts on this backend.</EmptyState>}
      <HistoricalRuns />
    </div>
  );
}
