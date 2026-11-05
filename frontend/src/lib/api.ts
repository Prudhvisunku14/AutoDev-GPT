import type {
  Analysis,
  Artifact,
  Approval,
  BackendStatus,
  Budget,
  Capabilities,
  GitHubStatus,
  ResourceCheck,
  ToolAnswer,
  ToolsListing,
  Criterion,
  FileContent,
  FileList,
  HistoryRun,
  Invocation,
  NewProject,
  Plan,
  Project,
  Report,
  Run,
  RunListItem,
  RunEvent,
} from "./types";

// Typed client for the existing FastAPI backend (/api/v1). Starting a run requires an explicit spending cap.

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The API token is missing or wrong. The backend always answers 401 for both. */
export class UnauthorizedError extends ApiError {
  constructor() {
    super(401, "The API token was not accepted.");
    this.name = "UnauthorizedError";
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  getToken: () => string | null;
  fetchImpl?: typeof fetch;
}

export const EVENT_PAGE = 500; // the backend's largest page
const UNREACHABLE = "The DevPilot backend could not be reached. Is it running?";
const MAX_EVENT_PAGES = 20; // 10,000 events: more than any run so far, and a bound on what one view loads

export function createApiClient({ baseUrl, getToken, fetchImpl }: ApiClientOptions) {
  const base = baseUrl.replace(/\/+$/, "");
  const doFetch = fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));

  async function send(path: string, init: { method?: "GET" | "POST"; body?: unknown; token?: string } = {}): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = init.token ?? getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (init.body !== undefined) headers["Content-Type"] = "application/json";
    let response: Response;
    try {
      response = await doFetch(`${base}${path}`, {
        method: init.method ?? "GET",
        headers,
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        cache: "no-store",
      });
    } catch {
      throw new ApiError(0, UNREACHABLE);
    }
    // The dev server's proxy marks its own failure (vite.config.ts), so it is not mistaken for a backend error.
    if (response.headers.get("X-DevPilot-Proxy") === "backend-unreachable") throw new ApiError(0, UNREACHABLE);
    if (response.status === 401) throw new UnauthorizedError();
    return response;
  }

  async function failure(response: Response, path: string): Promise<ApiError> {
    let detail = `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body) {
        const value = (body as { detail: unknown }).detail;
        detail = typeof value === "string" ? value : JSON.stringify(value);
      }
    } catch {
      // not JSON: the status says enough
    }
    return new ApiError(response.status, `${path}: ${detail}`);
  }

  async function json<T>(path: string, init?: { method?: "GET" | "POST"; body?: unknown; token?: string }): Promise<T> {
    const response = await send(path, init);
    if (!response.ok) throw await failure(response, path);
    return (await response.json()) as T;
  }

  /** The body, or null when the backend has none yet (404): no analysis, plan or report so far. */
  async function optional<T>(path: string): Promise<T | null> {
    const response = await send(path);
    if (response.status === 404) return null;
    if (!response.ok) throw await failure(response, path);
    return (await response.json()) as T;
  }

  async function blob(path: string): Promise<Blob> {
    const response = await send(path);
    if (!response.ok) throw await failure(response, path);
    return response.blob();
  }

  const id = (value: string) => encodeURIComponent(value);

  return {
    health: () => json<{ status: "ok" }>("/health"),
    status: () => json<BackendStatus>("/status"),
    /** /status with a token that is not stored yet, so a wrong one is never kept or sent by other requests. */
    checkToken: (token: string) => json<BackendStatus>("/status", { token }),
    listProjects: (limit = 200) => json<Project[]>(`/projects?limit=${limit}`),
    getBudget: () => json<Budget>("/budget"),
    getCapabilities: () => json<Capabilities>("/capabilities"),
    listTools: () => json<ToolsListing>("/tools"),
    /** Reads what the requirement names: public repositories at once, private ones through the GitHub connection. */
    checkResources: (projectId: string) => json<ResourceCheck>(`/projects/${id(projectId)}/resources/check`, { method: "POST" }),
    githubStatus: () => json<GitHubStatus>("/integrations/github"),
    /** The GitHub consent URL; the backend keeps the token, the dashboard never sees it. */
    connectGitHub: () => json<{ url: string }>("/integrations/github/connect", { method: "POST" }),
    disconnectGitHub: () => json<{ local_deleted: boolean; remote_revocation: string; note?: string }>("/integrations/github/disconnect", { method: "POST" }),
    authorizeGoogle: (projectId: string) => json<{ url: string; grant_id: string }>(`/projects/${id(projectId)}/resources/google/authorize`, { method: "POST" }),
    /** Answered from the backend's tool catalog: no model is called and nothing is spent. */
    askTools: (question: string) => json<ToolAnswer>("/tools/compare", { method: "POST", body: { question } }),
    getProject: (projectId: string) => json<Project>(`/projects/${id(projectId)}`),
    createProject: (project: NewProject) => json<Project>("/projects", { method: "POST", body: project }),
    listProjectFiles: (projectId: string) => json<FileList>(`/projects/${id(projectId)}/files`),
    readProjectFile: (projectId: string, path: string) => json<FileContent>(`/projects/${id(projectId)}/files/content?path=${encodeURIComponent(path)}`),
    listProjectRuns: (projectId: string) => json<Run[]>(`/projects/${id(projectId)}/runs`),
    listRuns: (limit = 200) => json<RunListItem[]>(`/runs?limit=${limit}`),
    createRun: (projectId: string) => json<Run>(`/projects/${id(projectId)}/runs`, { method: "POST" }),
    startRun: (runId: string, approvedMaxUsd: number) =>
      json<Run>(`/runs/${id(runId)}/start`, { method: "POST", body: { approved_max_usd: approvedMaxUsd } }),
    cancelRun: (runId: string) => json<Run>(`/runs/${id(runId)}/cancel`, { method: "POST" }),
    getRun: (runId: string) => json<Run>(`/runs/${id(runId)}`),
    getAnalysis: (runId: string) => optional<Analysis>(`/runs/${id(runId)}/analysis`),
    getPlan: (runId: string) => optional<Plan>(`/runs/${id(runId)}/plan`),
    getCriteria: (runId: string) => json<Criterion[]>(`/runs/${id(runId)}/criteria`),
    getInvocations: (runId: string) => json<Invocation[]>(`/runs/${id(runId)}/invocations`),
    getReport: (runId: string) => optional<Report>(`/runs/${id(runId)}/report`),
    getArtifact: (runId: string) => json<Artifact>(`/runs/${id(runId)}/artifact`),
    downloadArtifact: (runId: string) => blob(`/runs/${id(runId)}/artifact.zip`),
    getEvents: (runId: string, afterSeq = 0, limit = EVENT_PAGE) => json<RunEvent[]>(`/runs/${id(runId)}/events?after_seq=${afterSeq}&limit=${limit}`),
    /** Every event of a run, page by page, up to a fixed bound. */
    async getAllEvents(runId: string): Promise<RunEvent[]> {
      const events: RunEvent[] = [];
      let after = 0;
      for (let page = 0; page < MAX_EVENT_PAGES; page += 1) {
        const batch = await json<RunEvent[]>(`/runs/${id(runId)}/events?after_seq=${after}&limit=${EVENT_PAGE}`);
        events.push(...batch);
        const last = batch[batch.length - 1];
        if (batch.length < EVENT_PAGE || !last) break;
        after = last.seq;
      }
      return events;
    },
    listApprovals: (status: "pending" | "all" = "pending", runId?: string) =>
      json<Approval[]>(`/approvals?status=${status}${runId ? `&run_id=${id(runId)}` : ""}`),
    decideApproval: (approvalId: string, decision: "approve" | "deny", response?: string) =>
      json<Approval>(`/approvals/${id(approvalId)}/decision`, { method: "POST", body: { decision, response } }),
    listHistoryRuns: () => json<HistoryRun[]>("/history/runs"),
    getHistoryRun: (recordId: string) => json<HistoryRun>(`/history/runs/${id(recordId)}`),
    getHistoryArtifact: (recordId: string) => json<Artifact>(`/history/runs/${id(recordId)}/artifact`),
    downloadHistoryArtifact: (recordId: string) => blob(`/history/runs/${id(recordId)}/artifact.zip`),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
