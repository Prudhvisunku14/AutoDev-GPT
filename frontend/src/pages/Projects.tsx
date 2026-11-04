import { ChevronRight, FolderOpen, Loader2, Plus } from "lucide-react";
import { Link } from "react-router";
import { EmptyState } from "../components/EmptyState";
import { Notice } from "../components/Notice";
import { PageTitle } from "../components/PageTitle";
import { StatusBadge } from "../components/StatusBadge";
import { formatDate } from "../lib/format";
import { useAllRuns } from "../lib/queries";

export function Projects() {
  const { projects, entries, isLoading, error } = useAllRuns();

  if (isLoading) {
    return (
      <div className="mx-auto max-w-5xl pt-8">
        <PageTitle title="Projects" />
        <p className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden /> Loading projects…
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl pt-8">
      <PageTitle title="Projects" subtitle="Every requirement saved on this backend, with its latest run." />
      {error ? <Notice tone="error" title="Projects could not be loaded">{error instanceof Error ? error.message : "Unknown error"}</Notice> : null}
      {!projects.length ? (
        <EmptyState
          icon={FolderOpen}
          title="No projects yet"
          action={
            <Link to="/" state={{ focusComposer: true }} className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-2.5 text-sm font-semibold text-white">
              <Plus className="size-4" aria-hidden /> Describe your first application
            </Link>
          }
        >
          Describe an application on the home page and it is saved here as a project.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-white shadow-soft">
          {projects.map((project) => {
            const latest = entries.find((e) => e.project.id === project.id)?.run;
            return (
              <li key={project.id}>
                <Link to={`/projects/${project.id}`} className="flex items-center gap-4 px-5 py-4 transition hover:bg-violet-50/40 focus-visible:bg-violet-50/40">
                  <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-violet-600">
                    <FolderOpen className="size-5" aria-hidden />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium">{project.name}</p>
                    <p className="mt-0.5 truncate text-xs text-muted">
                      Created {formatDate(project.created_at)}
                      {project.stack ? ` · ${project.stack}` : " · stack chosen when planned"}
                    </p>
                  </div>
                  <div className="hidden shrink-0 sm:block">{latest ? <StatusBadge state={latest.state} paused={!!latest.pause_reason} /> : <span className="text-xs text-muted">No runs</span>}</div>
                  <span className="inline-flex shrink-0 items-center gap-1 text-sm font-medium text-violet-700">
                    Open <ChevronRight className="size-4" aria-hidden />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
