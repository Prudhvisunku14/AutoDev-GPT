import { ArrowLeft, ChevronRight, Loader2, Terminal } from "lucide-react";
import { Link, useLocation, useParams } from "react-router";
import { FileBrowser } from "../components/FileBrowser";
import { Notice } from "../components/Notice";
import { StatusBadge } from "../components/StatusBadge";
import { formatCost, formatDate, formatDateTime, shortId } from "../lib/format";
import { useProject, useProjectFiles, useProjectRuns } from "../lib/queries";

export function ProjectDetail() {
  const { projectId } = useParams();
  const location = useLocation();
  const created = (location.state as { created?: boolean } | null)?.created;
  const project = useProject(projectId);
  const runs = useProjectRuns(projectId);
  const files = useProjectFiles(projectId);

  if (project.isLoading) {
    return (
      <p className="flex items-center gap-2 pt-10 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Loading project…
      </p>
    );
  }
  if (!project.data) {
    return (
      <div className="mx-auto max-w-3xl pt-10">
        <Notice tone="error" title="Project not found">
          {project.error instanceof Error ? project.error.message : "It may have been removed."}
        </Notice>
      </div>
    );
  }

  const p = project.data;
  const list = runs.data ?? [];

  return (
    <div className="mx-auto max-w-5xl pt-6">
      <Link to="/projects" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden /> Projects
      </Link>
      <h1 className="mt-3 text-2xl font-bold tracking-tight">{p.name}</h1>
      <p className="mt-1 text-sm text-muted">
        Created {formatDate(p.created_at)} · {p.stack ?? "stack chosen when planned"} · {p.has_acceptance_suite ? "independent acceptance suite attached" : "no independent acceptance suite"}
      </p>

      {created && (
        <Notice tone="info" title="Project saved" className="mt-5">
          The requirement is stored on the backend. Review the build's spending cap before any model request begins.
        </Notice>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-6">
          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="text-sm font-semibold">Requirement</h2>
            <p className="mt-2 text-[15px] leading-relaxed whitespace-pre-wrap">{p.requirement_text}</p>
          </section>

          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="text-sm font-semibold">Runs</h2>
            {!list.length ? (
              <div className="mt-3 space-y-3">
                <p className="text-sm text-muted">No runs yet.</p>
                <div className="flex gap-3 rounded-xl bg-slate-50 p-3 text-[13px] text-slate-700">
                  <Terminal className="mt-0.5 size-4 shrink-0 text-violet-600" aria-hidden />
                  <p>
                    No run has been started for this project yet. New dashboard builds are started from the home page after an explicit spending approval.
                  </p>
                </div>
              </div>
            ) : (
              <ul className="mt-3 divide-y divide-line">
                {list.map((run) => (
                  <li key={run.id}>
                    <Link to={`/runs/${run.id}`} className="flex items-center gap-3 rounded-lg py-3 transition hover:bg-violet-50/40">
                      <StatusBadge state={run.state} paused={!!run.pause_reason} />
                      <span className="font-mono text-xs text-muted">{shortId(run.id)}</span>
                      <span className="hidden text-xs text-muted sm:inline">{formatDateTime(run.started_at ?? run.created_at)}</span>
                      <span className="ml-auto text-xs text-muted">{formatCost(run)}</span>
                      <ChevronRight className="size-4 text-slate-400" aria-hidden />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <aside className="rounded-2xl border border-line bg-white p-5 shadow-soft">
          <h2 className="mb-3 text-sm font-semibold">Files</h2>
          {files.isError ? <p className="text-[13px] text-rose-600">The files could not be listed.</p> : <FileBrowser projectId={p.id} paths={files.data?.files ?? []} />}
        </aside>
      </div>
    </div>
  );
}
