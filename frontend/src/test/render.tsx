// Test helpers only. Production code never imports this file.
import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";
import { AppRoutes } from "../App";
import { ApiProvider } from "../components/ApiProvider";
import { ApiError, type ApiClient } from "../lib/api";
import { createQueryClient } from "../lib/queries";
import { setToken } from "../lib/token";
import type { BackendStatus, Budget, Capabilities } from "../lib/types";
import { makeRun } from "./fixtures";

const STATUS: BackendStatus = {
  status: "ok",
  version: "0.6.0",
  phase: "test",
  database: "ok",
  workspaces_writable: true,
  egress_components: [],
  projects: 0,
  runs: 0,
};

export const DEFAULT_BUDGET: Budget = {
  cost_known: true,
  run_limit_usd: 2,
  coding_attempt_limit_usd: null,
  model: "test-model",
  price_input_per_mtok: 1,
  price_output_per_mtok: 1,
  max_turns: 60,
  max_tool_calls: 100,
  max_repair_attempts: 2,
  run_wall_clock_seconds: 1800,
  approval_required: true,
  session: null,
  recorded: { total_usd: 0.05, runs: 1, unknown_cost_runs: 0, active_runs: 0, active_committed_usd: 0 },
  reference: { finished_runs: 1, min_usd: 0.05, median_usd: 0.05, max_usd: 0.05 },
  estimate: "Exact build cost cannot be estimated reliably. The limit is enforced.",
};

export const DEFAULT_CAPABILITIES: Capabilities = {
  gpt: { ready: true, label: "FakeLLM (scripted)", detail: null, simulated: true },
  coding: { ready: true, label: "opencode", detail: null, simulated: false },
  sandbox: { ready: true, label: "DockerSandbox", detail: null, simulated: false },
  prices: { ready: true, label: "prices configured", detail: null, simulated: false },
  stacks: [
    { id: "python-cli", name: "Python command-line application", language: "python", frameworks: [], description: "Python 3.12 CLI.", verifiable: true, reason: "python-unittest runs in the approved runtime" },
    { id: "python-fastapi", name: "Python web API with FastAPI", language: "python", frameworks: ["fastapi"], description: "FastAPI service.", verifiable: false, reason: "runtime is not approved" },
    { id: "react-vite", name: "React single-page app with Vite", language: "typescript", frameworks: ["react", "vite"], description: "React front end.", verifiable: false, reason: "runtime is not approved" },
  ],
  can_build: true,
  problems: [],
  simulated: [],
  acceptance_suites: true,
  history: true,
};

function notFound(): Promise<never> {
  return Promise.reject(new ApiError(404, "not found"));
}

/** An ApiClient whose reads return an empty backend, with any method replaced by `overrides`. */
export function stubApi(overrides: Partial<ApiClient> = {}): ApiClient {
  const base: ApiClient = {
    health: vi.fn(() => Promise.resolve({ status: "ok" as const })),
    status: vi.fn(() => Promise.resolve(STATUS)),
    checkToken: vi.fn(() => Promise.resolve(STATUS)),
    signIn: vi.fn(() => Promise.resolve({ authenticated: true })),
    exchangeLogin: vi.fn(() => Promise.resolve({ authenticated: true })),
    getSession: vi.fn(() => Promise.resolve({ authenticated: false })),
    signOut: vi.fn(() => Promise.resolve({ authenticated: false })),
    listProjects: vi.fn(() => Promise.resolve([])),
    getBudget: vi.fn(() => Promise.resolve(DEFAULT_BUDGET)),
    getCapabilities: vi.fn(() => Promise.resolve(DEFAULT_CAPABILITIES)),
    listTools: vi.fn(() => Promise.resolve({ tasks: {}, tools: [], planned: [] })),
    checkResources: vi.fn(() => Promise.resolve({ resources: [], problems: [], needs_permission: [] })),
    githubStatus: vi.fn(() => Promise.resolve({ configured: false, blocker: "GITHUB_OAUTH_CLIENT_ID is not configured", connected: false, login: null, scopes: [], connected_at: null, requested_scopes: ["repo"] })),
    connectGitHub: vi.fn(notFound),
    n8nStatus: vi.fn(notFound),
    disconnectGitHub: vi.fn(notFound),
    authorizeGoogle: vi.fn(notFound),
    askTools: vi.fn(() => Promise.resolve({ kind: "recommendation" as const, recommendation: { tasks: [], picks: [], gaps: [] } })),
    getProject: vi.fn(notFound),
    createProject: vi.fn(notFound),
    listProjectFiles: vi.fn(() => Promise.resolve({ files: [] })),
    readProjectFile: vi.fn(notFound),
    listProjectRuns: vi.fn(() => Promise.resolve([])),
    listRuns: vi.fn(() => Promise.resolve([])),
    createRun: vi.fn(() => Promise.resolve(makeRun())),
    startRun: vi.fn(() => Promise.resolve(makeRun({ state: "ANALYZING", started_at: "2026-11-01T10:00:00Z" }))),
    cancelRun: vi.fn(() => Promise.resolve(makeRun({ state: "CANCELLED" }))),
    getRun: vi.fn(notFound),
    getAnalysis: vi.fn(() => Promise.resolve(null)),
    getPlan: vi.fn(() => Promise.resolve(null)),
    getCriteria: vi.fn(() => Promise.resolve([])),
    getInvocations: vi.fn(() => Promise.resolve([])),
    getReport: vi.fn(() => Promise.resolve(null)),
    getArtifact: vi.fn(() => Promise.resolve({ available: false, reason: "not verified", filename: null, files: [], excluded: [], total_bytes: 0, simulated: [] })),
    downloadArtifact: vi.fn(() => Promise.resolve(new Blob())),
    getEvents: vi.fn(() => Promise.resolve([])),
    getAllEvents: vi.fn(() => Promise.resolve([])),
    listApprovals: vi.fn(() => Promise.resolve([])),
    decideApproval: vi.fn(notFound),
    listHistoryRuns: vi.fn(() => Promise.resolve([])),
    getHistoryRun: vi.fn(notFound),
    getHistoryArtifact: vi.fn(() => Promise.resolve({ available: false, reason: "not verified", filename: null, files: [], excluded: [], total_bytes: 0, simulated: [] })),
    downloadHistoryArtifact: vi.fn(() => Promise.resolve(new Blob())),
  };
  return { ...base, ...overrides };
}

function testQueryClient() {
  const client = createQueryClient(); // keeps the real 401 handling
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
  return client;
}

interface Options {
  api?: ApiClient;
  route?: string | { pathname: string; state?: unknown };
  token?: string | null;
}

/** Renders `ui` with the providers the app uses. */
export function renderWithProviders(ui: ReactElement, { api = stubApi(), route = "/", token = "test-token" }: Options = {}) {
  if (token) setToken(token);
  const result = render(
    <ApiProvider client={api}>
      <QueryClientProvider client={testQueryClient()}>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </QueryClientProvider>
    </ApiProvider>,
  );
  return { ...result, api };
}

/** The whole app, with its layout and routes, at `route`. */
export function renderApp(options: Options = {}) {
  return renderWithProviders(<AppRoutes />, options);
}
