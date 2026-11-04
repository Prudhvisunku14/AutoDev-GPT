import type { Run, RunEvent, RunState } from "./types";
import { TERMINAL_STATES } from "./types";

// The five stages the Live Activity panel shows, worked out from the run's real state and its recorded events.
// Nothing here is estimated: a stage the backend has no evidence for stays pending, a step that did not happen is
// "skipped", and progress counts finished stages only.

export type StageKey = "analyze" | "retrieve" | "generate" | "test" | "deliver";
export type StageStatus = "pending" | "running" | "waiting" | "paused" | "completed" | "skipped" | "blocked" | "failed" | "cancelled";

export interface Stage {
  key: StageKey;
  label: string;
  status: StageStatus;
  summary: string;
  elapsedMs: number | null;
}

export interface StageView {
  stages: Stage[];
  finished: number; // completed or skipped
  total: number;
  active: boolean; // the run is working right now
  ended: RunState | null; // the run's terminal state, if it has one
}

export const STATUS_WORDS: Record<StageStatus, string> = {
  pending: "Pending",
  running: "Running",
  waiting: "Waiting",
  paused: "Paused",
  completed: "Done",
  skipped: "Skipped",
  blocked: "Blocked",
  failed: "Failed",
  cancelled: "Cancelled",
};

const LABELS: Record<StageKey, string> ={ analyze: "Analyze", retrieve: "Retrieve", generate: "Generate", test: "Test", deliver: "Deliver" };
const IDLE: Record<StageKey, string> = {
  analyze: "Understand the requirements",
  retrieve: "Search available project context",
  generate: "Create the application files",
  test: "Run verification",
  deliver: "Prepare the verified output",
};
const ORDER: StageKey[] = ["analyze", "retrieve", "generate", "test", "deliver"];

// Which stage each working state belongs to. Retrieval has no state of its own: it happens inside PLANNING.
const STAGE_OF: Partial<Record<RunState, StageKey>> = {
  ANALYZING: "analyze",
  PLANNING: "analyze",
  DEFINING_CRITERIA: "analyze",
  IMPLEMENTING: "generate",
  VERIFYING: "test",
  REPAIRING: "test",
  DOCUMENTING: "deliver",
};
const RETRIEVAL_EVENTS = new Set(["repository.searched", "repository.imported", "drive.discovered", "drive.ingested", "rag.indexed"]);
const RETRIEVAL_TOOLS = new Set(["search_repositories", "import_repository", "request_drive_authorization", "discover_drive_documents", "ingest_drive_documents", "build_rag_index"]);
const ANALYZE_RUNNING: Partial<Record<RunState, string>> = {
  ANALYZING: "Understanding the requirements",
  PLANNING: "Planning the tasks",
  DEFINING_CRITERIA: "Defining acceptance criteria",
};

function payloadOf(events: RunEvent[], type: string): Record<string, unknown>[] {
  return events.filter((e) => e.type === type).map((e) => e.payload);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

/** The working state the run was in when it ended, paused or began waiting for a person. */
function workingState(run: Run, events: RunEvent[]): RunState | null {
  if (run.state === "AWAITING_APPROVAL") return run.resume_state;
  if (!TERMINAL_STATES.has(run.state)) return run.state;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (!event) continue;
    const previous = str(event.payload.previous_state);
    if (previous && ["run.failed", "run.cancelled", "run.stopped", "run.completed"].includes(event.type)) return previous as RunState;
    if (event.type === "run.state_changed" && str(event.payload.to) === run.state) {
      const from = str(event.payload.from);
      if (from && from !== "AWAITING_APPROVAL") return from as RunState;
    }
  }
  return null;
}

/** When the run entered and left the states of one stage, from its state-change events. */
function window_(events: RunEvent[], key: StageKey): { start: number | null; end: number | null } {
  let start: number | null = null;
  let end: number | null = null;
  for (const event of events) {
    if (event.type !== "run.state_changed") continue;
    const to = str(event.payload.to) as RunState | null;
    const from = str(event.payload.from) as RunState | null;
    const at = Date.parse(event.ts);
    if (Number.isNaN(at)) continue;
    if (to && STAGE_OF[to] === key && start === null) start = at;
    if (from && STAGE_OF[from] === key && to && STAGE_OF[to] !== key && to !== "AWAITING_APPROVAL") end = at;
  }
  return { start, end };
}

function elapsed(events: RunEvent[], key: StageKey): number | null {
  const { start, end } = window_(events, key);
  return start !== null && end !== null && end >= start ? end - start : null;
}

function retrievalElapsed(events: RunEvent[]): number | null {
  const times = events
    .filter((e) => RETRIEVAL_EVENTS.has(e.type) || (e.type === "tool.started" && RETRIEVAL_TOOLS.has(String(e.payload.tool))))
    .map((e) => Date.parse(e.ts))
    .filter((t) => !Number.isNaN(t));
  return times.length > 1 ? Math.max(...times) - Math.min(...times) : null;
}

function analyzeSummary(events: RunEvent[]): string {
  const analysis = payloadOf(events, "analysis.accepted").at(-1);
  const plan = payloadOf(events, "plan.accepted").at(-1);
  const parts: string[] = [];
  const requirements = num(analysis?.requirements);
  const tasks = num(plan?.tasks);
  if (requirements !== null) parts.push(`${requirements} requirement${requirements === 1 ? "" : "s"}`);
  if (tasks !== null) parts.push(`${tasks} task${tasks === 1 ? "" : "s"} planned${plan?.stack ? ` on ${String(plan.stack)}` : ""}`);
  return parts.length ? `Requirements understood: ${parts.join(", ")}` : "Requirements understood";
}

function retrievalSummary(events: RunEvent[]): string {
  if (events.some((e) => e.type === "repository.imported")) return "Imported code from a public repository";
  if (events.some((e) => e.type === "repository.searched")) return "Searched public repositories";
  if (events.some((e) => e.type === "rag.indexed")) return "Indexed the project documents";
  return "Read the authorized documents";
}

function generateSummary(events: RunEvent[]): string {
  const finished = payloadOf(events, "coding.finished");
  const kept = finished.filter((f) => f.kept === true);
  const files = new Set(kept.flatMap((f) => (Array.isArray(f.changes) ? f.changes : []).map((c) => String((c as { path?: unknown }).path))));
  if (!kept.length) return "Application files created";
  return `${kept.length} coding attempt${kept.length === 1 ? "" : "s"} kept, ${files.size} file${files.size === 1 ? "" : "s"} changed`;
}

function testSummary(events: RunEvent[]): string | null {
  const last = payloadOf(events, "verification.finished").at(-1);
  const acceptance = payloadOf(events, "acceptance.finished").at(-1);
  const parts: string[] = [];
  if (last) parts.push(`${num(last.passed) ?? 0} of ${num(last.ran) ?? 0} tests passed`);
  if (acceptance) parts.push(`acceptance ${String(acceptance.verdict)} (${num(acceptance.passed) ?? 0} of ${num(acceptance.total) ?? 0})`);
  return parts.length ? parts.join("; ") : null;
}

export function deriveStages(run: Run | null, events: RunEvent[]): StageView {
  const stages: Stage[] = ORDER.map((key) => ({ key, label: LABELS[key], status: "pending", summary: IDLE[key], elapsedMs: null }));
  const at = (key: StageKey) => stages[ORDER.indexOf(key)] as Stage;
  if (!run) return { stages, finished: 0, total: ORDER.length, active: false, ended: null };

  const ended = TERMINAL_STATES.has(run.state) ? run.state : null;
  const working = workingState(run, events);
  const reached = run.state === "COMPLETED" ? ORDER.length : working && STAGE_OF[working] ? ORDER.indexOf(STAGE_OF[working] as StageKey) : -1;
  const active = !ended && !run.pause_reason && run.state !== "AWAITING_APPROVAL" && run.state !== "CREATED";

  // Everything before the stage the run reached is done; that stage carries the run's own condition.
  for (const key of ORDER) {
    const index = ORDER.indexOf(key);
    const stage = at(key);
    if (key === "retrieve") continue; // decided from evidence below
    if (index < reached) {
      stage.status = "completed";
      stage.elapsedMs = elapsed(events, key);
    } else if (index === reached) {
      stage.elapsedMs = elapsed(events, key);
      if (ended === "FAILED" || ended === "STOPPED_BY_POLICY") stage.status = "failed";
      else if (ended === "CANCELLED") stage.status = "cancelled";
      else if (run.state === "AWAITING_APPROVAL") stage.status = "waiting";
      else if (run.pause_reason) stage.status = "paused";
      else stage.status = "running";
    }
  }

  // Summaries from what the events actually say.
  const analyze = at("analyze");
  if (analyze.status === "completed") analyze.summary = analyzeSummary(events);
  else if (analyze.status === "running" && working) analyze.summary = ANALYZE_RUNNING[working] ?? analyze.summary;

  const generate = at("generate");
  if (generate.status === "completed") generate.summary = generateSummary(events);
  else if (generate.status === "running") {
    const started = payloadOf(events, "coding.started").at(-1);
    generate.summary = started ? `Creating application files (task ${String(started.task ?? started.task_order ?? "?")})` : "Creating application files";
  }

  const test = at("test");
  const tested = testSummary(events);
  if (tested && (test.status === "completed" || test.status === "running" || test.status === "failed")) test.summary = tested;
  else if (test.status === "running") test.summary = "Running verification";

  const deliver = at("deliver");
  const delivered = payloadOf(events, "delivery.finished").at(-1);
  if (delivered) {
    deliver.status = "completed";
    const files = Array.isArray(delivered.files) ? delivered.files.length : null;
    deliver.summary = files === null ? "Verified output delivered" : `${files} verified file${files === 1 ? "" : "s"} delivered`;
  } else if (deliver.status === "running") deliver.summary = "Preparing the verified output";

  // A stack with no approved verification runtime stops the run before any code is written.
  const blocked = payloadOf(events, "verification.blocked").at(-1);
  if (blocked) {
    generate.status = "blocked";
    generate.summary = "Stopped before any code was written: verification is unavailable for this stack";
    test.status = "blocked";
    const components = Array.isArray(blocked.components) ? (blocked.components as Array<{ reason?: unknown }>) : [];
    test.summary = str(components[0]?.reason) ?? "No approved verification runtime";
  }

  // Failure details belong on the stage that failed.
  const failedStage = stages.find((s) => s.status === "failed" || s.status === "cancelled");
  if (failedStage) failedStage.summary = run.status_detail || (failedStage.status === "cancelled" ? "The run was cancelled here" : `The run ended here (${run.outcome ?? run.state})`);
  const pausedStage = stages.find((s) => s.status === "paused");
  if (pausedStage) pausedStage.summary = `Paused: ${run.status_detail || run.pause_reason}`;
  const waitingStage = stages.find((s) => s.status === "waiting");
  if (waitingStage) waitingStage.summary = "Waiting for a person's decision";

  // Retrieval: evidence, a tool still running, or skipped once planning is over.
  const retrieve = at("retrieve");
  const evidence = events.some((e) => RETRIEVAL_EVENTS.has(e.type));
  const finishedCalls = new Set(events.filter((e) => e.type === "tool.result").map((e) => String(e.payload.call_id)));
  const retrieving = events.some((e) => e.type === "tool.started" && RETRIEVAL_TOOLS.has(String(e.payload.tool)) && !finishedCalls.has(String(e.payload.call_id)));
  if (retrieving && active) {
    retrieve.status = "running";
    retrieve.summary = "Searching available project context";
  } else if (evidence) {
    retrieve.status = "completed";
    retrieve.summary = retrievalSummary(events);
    retrieve.elapsedMs = retrievalElapsed(events);
  } else if (reached >= ORDER.indexOf("generate") || at("analyze").status === "completed") {
    retrieve.status = "skipped";
    retrieve.summary = "Not used in this run";
  } else if (ended && reached <= 0) {
    retrieve.summary = "Not reached";
  }

  // Stages after a failure did not run; say so instead of leaving them looking queued.
  if (ended && ended !== "COMPLETED") {
    for (const stage of stages) if (stage.status === "pending") stage.summary = "Not reached";
  }

  const finished = stages.filter((s) => s.status === "completed" || s.status === "skipped").length;
  return { stages, finished, total: ORDER.length, active, ended };
}
