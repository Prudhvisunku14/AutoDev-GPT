// The backend's response shapes (backend/app/schemas/api.py and app/verification/report.py). Field names match the
// JSON exactly. Only what the dashboard reads is typed in detail; the rest stays `unknown`.

export type RunState =
  | "CREATED"
  | "ANALYZING"
  | "PLANNING"
  | "DEFINING_CRITERIA"
  | "IMPLEMENTING"
  | "VERIFYING"
  | "REPAIRING"
  | "DOCUMENTING"
  | "COMPLETED"
  | "AWAITING_APPROVAL"
  | "FAILED"
  | "CANCELLED"
  | "STOPPED_BY_POLICY";

export const TERMINAL_STATES: ReadonlySet<RunState> = new Set(["COMPLETED", "FAILED", "CANCELLED", "STOPPED_BY_POLICY"]);

export interface Project {
  id: string;
  name: string;
  requirement_text: string;
  stack: string | null;
  status: "ACTIVE" | "ARCHIVED";
  created_at: string;
  has_acceptance_suite: boolean;
}

export interface RunListItem extends Run {
  project_name: string;
}

export interface Run {
  id: string;
  project_id: string;
  state: RunState;
  resume_state: RunState | null;
  turns: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  outcome: string | null;
  tool_calls: number;
  cost_known: boolean;
  active_seconds: number;
  pause_reason: string | null;
  status_detail: string | null;
  acceptance_sha256?: string | null;
}

export interface RunEvent {
  seq: number;
  type: string;
  project_id: string;
  run_id: string | null;
  payload: Record<string, unknown>;
  ts: string;
}

export interface Requirement {
  code: string;
  text: string;
  kind: string;
}

export interface Analysis {
  summary: string;
  requirements: Requirement[];
  assumptions: string[];
  ambiguities: string[];
}

export interface PlanTask {
  order: number;
  title: string;
  description: string;
  capability: string;
  requirement_refs: string[];
  features: Record<string, unknown>;
  status: string;
}

export interface Plan {
  stack: string;
  stack_rationale: string;
  summary: string;
  tasks: PlanTask[];
}

export interface Criterion {
  code: string;
  given: string;
  when: string;
  then: string;
  priority: string;
  method: string;
  requirement_refs: string[];
  status: string;
}

export interface Invocation {
  seq: number;
  call_id: string;
  tool: string;
  state_at_call: string;
  status: string;
  ok: boolean | null;
  error_class: string | null;
  arguments: unknown;
  output: unknown;
  latency_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface Approval {
  id: string;
  run_id: string;
  kind: string;
  tool: string;
  risk: string;
  payload: unknown;
  details: unknown;
  status: string;
  response_text: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface BackendStatus {
  status: "ok" | "degraded";
  version: string;
  phase: string;
  database: "ok" | "error";
  workspaces_writable: boolean;
  egress_components: string[];
  projects: number;
  runs: number;
}

export interface Budget {
  cost_known: boolean;
  run_limit_usd: number;
  coding_attempt_limit_usd: number | null;
  model: string | null;
  price_input_per_mtok: number | null;
  price_output_per_mtok: number | null;
  max_turns: number;
  max_tool_calls: number;
  max_repair_attempts: number;
  run_wall_clock_seconds: number;
  approval_required: boolean;
  session: { approved_total_usd: number; committed_usd: number; remaining_usd: number; runs_admitted: number; seconds_left: number | null } | null;
  recorded: { total_usd: number; runs: number; unknown_cost_runs: number; active_runs: number; active_committed_usd: number };
  reference: { finished_runs: number; min_usd: number | null; median_usd: number | null; max_usd: number | null };
  estimate: string;
}

export interface StackCapability {
  id: string;
  name: string;
  language: string;
  frameworks: string[];
  description: string;
  verifiable: boolean;
  reason: string;
}

export interface CapabilityPart {
  ready: boolean;
  label: string;
  detail: string | null;
  simulated: boolean;
}

export interface Capabilities {
  gpt: CapabilityPart;
  coding: CapabilityPart;
  sandbox: CapabilityPart;
  prices: CapabilityPart;
  stacks: StackCapability[];
  can_build: boolean;
  problems: string[];
  simulated: string[];
  acceptance_suites: boolean;
  history: boolean;
}

export interface Artifact {
  available: boolean;
  reason: string | null;
  /** How a downloadable delivery was verified: the evaluator's independent suite, or the project's own tests only. */
  verification?: "independent" | "generated-tests" | null;
  filename: string | null;
  files: Array<{ path: string; bytes: number }>;
  excluded: Array<{ path: string; reason: string }>;
  total_bytes: number;
  simulated: string[];
}

export interface HistoryRun {
  id: string;
  recorded_at: string;
  source: string;
  project_name: string | null;
  requirement: string | null;
  run_id: string | null;
  state: string | null;
  outcome: string | null;
  detail: string | null;
  cost_usd: number | null;
  tokens: number | null;
  seconds: number | null;
  turns: number | null;
  delivered: string[];
  report: Report | null;
}

export interface FileList {
  files: string[];
}

export interface FileContent {
  path: string;
  content: string;
}

export interface VerificationSummary {
  verdict: string;
  passed: number;
  ran: number;
  failed_names?: string[];
  reason?: string;
  container?: string;
}

export interface AcceptanceCriterionResult {
  code: string;
  description?: string;
  status: string;
  priority?: string;
}

export interface AcceptanceSummary {
  configured: boolean;
  verdict?: string;
  reason?: string;
  passed?: number;
  total?: number;
  note?: string;
  suite_sha256?: string | null;
  criteria?: AcceptanceCriterionResult[];
}

export interface DeliveredFile {
  path: string;
  change: string;
  bytes: number;
}

export interface Report {
  project: { id: string; name: string; requirement: string; stack: string | null };
  run: { id: string; state: RunState; outcome: string | null; detail: string | null; turns: number; tokens: number; cost_usd: number | null; repairs: number };
  tasks: Array<{ order: number; title: string; status: string; component?: string | null }>;
  verifications: VerificationSummary[];
  acceptance: AcceptanceSummary;
  delivered_files: DeliveredFile[];
  real_workspace_changed: boolean;
  provenance: { gpt: string; coding_providers: string[]; sandbox: string; simulated: string[]; fully_real: boolean };
  selection?: Array<{ provider?: string; simulated?: boolean; task?: number }>;
}

export interface NewProject {
  name: string;
  requirement_text: string;
  stack?: string;
}

// --- The tool catalog (GET /tools, POST /tools/compare) ---------------------------------------------------------------

export type ToolStatus = "ready" | "simulated" | "not_configured" | "blocked" | "planned";

export interface ToolEvidence {
  level: "documentation" | "offline_test" | "real_container" | "live";
  outcome: "passed" | "failed" | "partial";
  date: string;
  summary: string;
  source: string;
}

export interface ToolEntry {
  id: string;
  name: string;
  role: string;
  description?: string;
  tasks: Array<{ id: string; text: string }> | string[];
  advantages: string[];
  limitations: string[];
  requirements?: string[];
  readiness: { status: ToolStatus; detail: string };
  functions: string[];
  evidence?: ToolEvidence[];
  strongest_evidence: ToolEvidence | null;
  verified_live: boolean;
}

export interface PlannedToolEntry {
  id: string;
  name: string;
  description: string;
  readiness: { status: "planned"; detail: string };
}

export interface ToolsListing {
  tasks: Record<string, string>;
  tools: ToolEntry[];
  planned: PlannedToolEntry[];
}

export interface ToolPick {
  task: string;
  text: string;
  tool: string;
  name: string;
  readiness: ToolStatus;
  alternatives: string[];
  why: string;
}

export interface ToolRecommendation {
  tasks: string[];
  picks: ToolPick[];
  gaps: Array<{ task: string; text: string; note: string }>;
}

export interface ToolAnswer {
  kind: "comparison" | "recommendation";
  question?: string;
  tools?: ToolEntry[];
  unknown?: Array<{ name: string; note: string }>;
  planned?: Array<{ name: string; note: string }>;
  shared_tasks?: string[];
  unique_tasks?: Record<string, string[]>;
  notes?: string[];
  recommendation?: ToolRecommendation;
  note?: string | null;
}

// --- Resources a request names, and the GitHub connection ---------------------------------------------------------------

export type ResourceAction = "connect_github" | "connect_google" | null;

export interface ResourceItem {
  kind: "github" | "drive";
  key: string;
  url: string;
  label: string;
  status: string;
  message: string;
  action: ResourceAction;
  full_name?: string;
  visibility?: "public" | "private";
  license?: string | null;
  stack?: string | null;
  summary?: string;
}

export interface ResourceCheck {
  resources: ResourceItem[];
  problems: string[];
  needs_permission: string[];
}

export interface GitHubStatus {
  configured: boolean;
  blocker: string | null;
  connected: boolean;
  login: string | null;
  scopes: string[];
  connected_at: number | null;
  requested_scopes: string[];
}

export interface N8nWorkflowHealth {
  key: string;
  path: string;
  description: string;
  effect: string;
  found: boolean | null;
  active: boolean | null;
  name?: string | null;
  recent_executions?: Array<{ id: string; status: string; mode: string; started_at: string | null; stopped_at: string | null }>;
}

export interface N8nHealth {
  source: string;
  simulated: boolean;
  problems: string[];
  reachable: boolean;
  ready: boolean;
  error: string | null;
  workflows: N8nWorkflowHealth[];
}

// --- Draft pull request of a verified delivery (POST /evaluation/github/preview, /publish) ------------------------------

export interface GithubPRPreview {
  preview_id: string;
  expires_in_seconds: number;
  verification_level: string;
  confirmation_required: string;
  github_account: string | null;
  preview: {
    run_id: string;
    repository: string;
    branch: string;
    base: string;
    title: string;
    total_bytes: number;
    fingerprint: string;
    files: { path: string; bytes: number; sha256: string }[];
  };
}

export interface GithubPRResult {
  url: string;
  branch: string;
  commit: string;
  repository: string;
  draft: string;
}
