import { QueryCache, QueryClient, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, UnauthorizedError } from "./api";
import { useApi } from "./apiContext";
import { clearToken, useToken } from "./token";
import type { NewProject, Project, Run, RunEvent } from "./types";
import { TERMINAL_STATES } from "./types";

// Every read the pages make, through TanStack Query. A run that is working is polled every 2 s; one waiting for a
// person every 10 s; anything else is not polled. Polling stops while the tab is hidden (TanStack's default).

export const ACTIVE_POLL_MS = 2_000;
export const WAITING_POLL_MS = 10_000;
export const LIST_POLL_MS = 15_000;
const RUNS_PER_PAGE_LIMIT = 50; // the Runs page asks for reports of at most this many finished runs

export function isWorking(run: Run | null | undefined): boolean {
  return !!run && !TERMINAL_STATES.has(run.state) && !run.pause_reason && run.state !== "AWAITING_APPROVAL" && run.state !== "CREATED";
}

export function pollFor(run: Run | null | undefined): number | false {
  if (isWorking(run)) return ACTIVE_POLL_MS;
  if (run?.state === "AWAITING_APPROVAL") return WAITING_POLL_MS;
  return false;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (error instanceof UnauthorizedError) clearToken(); // the page asks for the token again
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        retry: (failures, error) => !(error instanceof ApiError && [0, 401, 403, 404, 422].includes(error.status)) && failures < 1,
      },
    },
  });
}

export const keys = {
  status: ["status"] as const,
  budget: ["budget"] as const,
  capabilities: ["capabilities"] as const,
  projects: ["projects"] as const,
  runs: ["runs"] as const,
  project: (id: string) => ["project", id] as const,
  projectRuns: (id: string) => ["project", id, "runs"] as const,
  projectFiles: (id: string) => ["project", id, "files"] as const,
  file: (id: string, path: string) => ["project", id, "file", path] as const,
  run: (id: string) => ["run", id] as const,
  events: (id: string) => ["run", id, "events"] as const,
  report: (id: string) => ["run", id, "report"] as const,
  plan: (id: string) => ["run", id, "plan"] as const,
  analysis: (id: string) => ["run", id, "analysis"] as const,
  approvals: ["approvals"] as const,
  history: ["history", "runs"] as const,
  artifact: (id: string) => ["run", id, "artifact"] as const,
};

export function useConnected(): boolean {
  return !!useToken();
}

export function useStatus() {
  const api = useApi();
  return useQuery({ queryKey: keys.status, queryFn: api.status, enabled: useConnected() });
}

export function useBudget() {
  const api = useApi();
  return useQuery({ queryKey: keys.budget, queryFn: api.getBudget, enabled: useConnected(), refetchInterval: LIST_POLL_MS });
}

export function useCapabilities() {
  const api = useApi();
  return useQuery({ queryKey: keys.capabilities, queryFn: api.getCapabilities, enabled: useConnected(), staleTime: 30_000 });
}

export function useProjects() {
  const api = useApi();
  return useQuery({ queryKey: keys.projects, queryFn: () => api.listProjects(), enabled: useConnected(), refetchInterval: LIST_POLL_MS });
}

export function useProject(id: string | undefined) {
  const api = useApi();
  return useQuery({ queryKey: keys.project(id ?? ""), queryFn: () => api.getProject(id as string), enabled: useConnected() && !!id });
}

export function useProjectRuns(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: keys.projectRuns(id ?? ""),
    queryFn: () => api.listProjectRuns(id as string),
    enabled: useConnected() && !!id,
    refetchInterval: (query) => ((query.state.data ?? []).some(isWorking) ? ACTIVE_POLL_MS : LIST_POLL_MS),
  });
}

export interface RunEntry {
  project: Pick<Project, "id" | "name">;
  run: Run;
}

/** Every run, newest first, from the backend's authenticated all-runs endpoint. */
export function useAllRuns(): { entries: RunEntry[]; isLoading: boolean; error: unknown; projects: Project[] } {
  const api = useApi();
  const projects = useProjects();
  const runs = useQuery({
    queryKey: keys.runs,
    queryFn: () => api.listRuns(),
    enabled: useConnected(),
    refetchInterval: (query) => (query.state.data?.some(isWorking) ? ACTIVE_POLL_MS : LIST_POLL_MS),
  });
  const entries = (runs.data ?? []).map(({ project_name, ...run }) => ({
    project: { id: run.project_id, name: project_name },
    run,
  }));
  return {
    entries,
    isLoading: runs.isLoading || projects.isLoading,
    error: runs.error ?? projects.error ?? null,
    projects: projects.data ?? [],
  };
}

export function useRun(id: string | undefined) {
  const api = useApi();
  return useQuery({ queryKey: keys.run(id ?? ""), queryFn: () => api.getRun(id as string), enabled: useConnected() && !!id, refetchInterval: (q) => pollFor(q.state.data) });
}

export function useRunEvents(run: Run | null | undefined) {
  const api = useApi();
  const client = useQueryClient();
  return useQuery({
    queryKey: keys.events(run?.id ?? ""),
    queryFn: async () => {
      const id = (run as Run).id;
      const previous = client.getQueryData<RunEvent[]>(keys.events(id)) ?? [];
      if (!previous.length) return api.getAllEvents(id);
      const latest = previous.at(-1)?.seq ?? 0;
      return [...previous, ...(await api.getEvents(id, latest))];
    },
    enabled: useConnected() && !!run,
    refetchInterval: pollFor(run),
  });
}

export function useReport(run: Run | null | undefined) {
  const api = useApi();
  const ended = !!run && TERMINAL_STATES.has(run.state);
  return useQuery({ queryKey: keys.report(run?.id ?? ""), queryFn: () => api.getReport((run as Run).id), enabled: useConnected() && ended, staleTime: 60_000 });
}

/** Reports of finished runs, for the Runs page's provider and test columns. Active runs have none yet. */
export function useReports(runs: Run[]) {
  const api = useApi();
  const connected = useConnected();
  const finished = runs.filter((r) => TERMINAL_STATES.has(r.state)).slice(0, RUNS_PER_PAGE_LIMIT);
  const results = useQueries({
    queries: finished.map((run) => ({ queryKey: keys.report(run.id), queryFn: () => api.getReport(run.id), enabled: connected, staleTime: 60_000 })),
  });
  return new Map(finished.map((run, index) => [run.id, results[index]?.data ?? null]));
}

// The plan and the analysis are asked for only once the run's events say they were accepted. Before that the backend
// answers 404, correctly, and the browser logs every such answer to the console, on every poll of a working run.

export function usePlan(run: Run | null | undefined, accepted: boolean) {
  const api = useApi();
  return useQuery({ queryKey: keys.plan(run?.id ?? ""), queryFn: () => api.getPlan((run as Run).id), enabled: useConnected() && !!run && accepted, refetchInterval: pollFor(run) });
}

export function useAnalysis(run: Run | null | undefined, accepted: boolean) {
  const api = useApi();
  return useQuery({ queryKey: keys.analysis(run?.id ?? ""), queryFn: () => api.getAnalysis((run as Run).id), enabled: useConnected() && !!run && accepted, refetchInterval: pollFor(run) });
}

export function useProjectFiles(id: string | undefined, poll: number | false = false) {
  const api = useApi();
  return useQuery({ queryKey: keys.projectFiles(id ?? ""), queryFn: () => api.listProjectFiles(id as string), enabled: useConnected() && !!id, refetchInterval: poll });
}

export function useFileContent(projectId: string | undefined, path: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: keys.file(projectId ?? "", path ?? ""),
    queryFn: () => api.readProjectFile(projectId as string, path as string),
    enabled: useConnected() && !!projectId && !!path,
  });
}

export function useApprovals() {
  const api = useApi();
  return useQuery({ queryKey: keys.approvals, queryFn: () => api.listApprovals("pending"), enabled: useConnected(), refetchInterval: LIST_POLL_MS });
}

export function useRunApprovals(runId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: [...keys.approvals, runId ?? ""],
    queryFn: () => api.listApprovals("pending", runId),
    enabled: useConnected() && !!runId,
    refetchInterval: WAITING_POLL_MS,
  });
}

export function useArtifact(run: Run | null | undefined) {
  const api = useApi();
  const ended = !!run && TERMINAL_STATES.has(run.state);
  return useQuery({
    queryKey: keys.artifact(run?.id ?? ""),
    queryFn: () => api.getArtifact((run as Run).id),
    enabled: useConnected() && ended,
    staleTime: 60_000,
  });
}

export function useHistoryRuns() {
  const api = useApi();
  return useQuery({ queryKey: keys.history, queryFn: api.listHistoryRuns, enabled: useConnected(), refetchInterval: LIST_POLL_MS });
}

export function useHistoryRun(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: [...keys.history, id ?? ""],
    queryFn: () => api.getHistoryRun(id as string),
    enabled: useConnected() && !!id,
    staleTime: 60_000,
  });
}

export function useHistoryArtifact(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: ["history", "artifact", id ?? ""],
    queryFn: () => api.getHistoryArtifact(id as string),
    enabled: useConnected() && !!id,
    staleTime: 60_000,
  });
}

export function useCreateProject() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (project: NewProject) => api.createProject(project),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.projects }),
  });
}
