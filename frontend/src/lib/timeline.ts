import type { RunEvent } from "./types";

// A run's event log, turned into the few lines a person wants to read. Polling and bookkeeping events are dropped,
// and repeats are folded: a dozen engine tool calls become one line with counts. Nothing is invented: every line
// comes from one or more recorded events.

export type TimelineKind = "state" | "gpt" | "tool" | "coding" | "files" | "test" | "recovery" | "error" | "delivery" | "milestone" | "info";
export type Tone = "neutral" | "good" | "bad" | "warn";

export interface TimelineItem {
  key: string;
  kind: TimelineKind;
  title: string;
  detail?: string;
  ts: string;
  tone: Tone;
}

const HIDDEN = new Set([
  "llm.request",
  "tool.called",
  "tool.started",
  "run.start_requested",
  "coding.attempt_phase",
  "coding.prompt_sent",
  "run.created",
  "project.created",
]);
const STATE_WORDS: Record<string, string> = {
  ANALYZING: "Analyzing the requirement",
  PLANNING: "Planning",
  DEFINING_CRITERIA: "Defining acceptance criteria",
  IMPLEMENTING: "Coding",
  VERIFYING: "Testing",
  REPAIRING: "Repairing",
  DOCUMENTING: "Preparing delivery",
  COMPLETED: "Completed",
  AWAITING_APPROVAL: "Waiting for a person's decision",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  STOPPED_BY_POLICY: "Stopped by a limit or policy",
};

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : value === undefined || value === null ? fallback : String(value);
}

function count(value: unknown): number {
  return typeof value === "number" ? value : Array.isArray(value) ? value.length : 0;
}

function describe(event: RunEvent): Omit<TimelineItem, "key" | "ts"> | null {
  const p = event.payload;
  switch (event.type) {
    case "run.started":
      return { kind: "state", title: "Run started", tone: "neutral" };
    case "run.state_changed": {
      const to = text(p.to);
      return { kind: "state", title: STATE_WORDS[to] ?? to, tone: to === "COMPLETED" ? "good" : ["FAILED", "STOPPED_BY_POLICY"].includes(to) ? "bad" : "neutral" };
    }
    case "llm.response": {
      const calls = Array.isArray(p.function_calls) ? (p.function_calls as unknown[]).map((c) => text(c)) : [];
      const tokens = `${count(p.input_tokens)} in / ${count(p.output_tokens)} out tokens`;
      const cost = typeof p.cost_usd === "number" ? `, $${p.cost_usd.toFixed(4)}` : "";
      return { kind: "gpt", title: calls.length ? `GPT chose ${calls.join(", ")}` : "GPT replied without a tool call", detail: `${tokens}${cost}`, tone: "neutral" };
    }
    case "tool.result":
      return { kind: "tool", title: `${text(p.tool, "A tool")} ${p.ok === false ? "was refused" : "succeeded"}`, tone: p.ok === false ? "warn" : "neutral" };
    case "analysis.accepted":
      return { kind: "milestone", title: `Analysis accepted: ${count(p.requirements)} requirements`, tone: "good" };
    case "plan.accepted":
      return { kind: "milestone", title: `Plan accepted: ${count(p.tasks)} tasks on ${text(p.stack, "?")}`, tone: "good" };
    case "criteria.accepted":
      return { kind: "milestone", title: `Acceptance criteria accepted: ${count(p.criteria)} (${count(p.must)} MUST)`, tone: "good" };
    case "selection.chosen":
      return { kind: "coding", title: `Task ${text(p.task, "?")}: ${text(p.provider, "a provider")} chosen${p.simulated ? " (simulated)" : ""}`, tone: "neutral" };
    case "coding.started":
      return { kind: "coding", title: `Coding task ${text(p.task ?? p.task_order, "?")} started`, detail: text(p.title) || undefined, tone: "neutral" };
    case "coding.finished": {
      const kept = p.kept === true;
      const changes = Array.isArray(p.changes) ? p.changes.length : 0;
      return {
        kind: "coding",
        title: `Coding task ${text(p.task_order, "?")}: ${text(p.status)}${kept ? `, kept (${changes} file${changes === 1 ? "" : "s"})` : ", undone"}`,
        detail: text(p.detail) || text(p.stop) || undefined,
        tone: kept ? "good" : "bad",
      };
    }
    case "acceptance.frozen":
      return { kind: "milestone", title: `Browser acceptance checks frozen before coding: ${count(p.checks)}`, detail: "Written from the requirements; the coding tool never sees them.", tone: "good" };
    case "verification.started":
      return p.backend === "website"
        ? { kind: "test", title: "Website checks started in Docker (no network)", detail: p.independent_acceptance ? `Built-in browser checks plus ${count(p.acceptance_checks)} frozen acceptance checks` : "Built-in browser checks only", tone: "neutral" }
        : { kind: "test", title: "Tests started in the sandbox", tone: "neutral" };
    case "verification.website": {
      const stages = Array.isArray(p.stages) ? (p.stages as { name?: unknown; ok?: unknown }[]) : [];
      const failedStages = stages.filter((stage) => stage.ok !== true).map((stage) => text(stage.name));
      const pages = Array.isArray(p.pages) ? p.pages.length : 0;
      return {
        kind: "test",
        title: `Browser run ${text(p.verdict)}: ${pages} page${pages === 1 ? "" : "s"} rendered in Chromium`,
        detail: failedStages.length ? `Failed: ${failedStages.join(", ")}. ${text(p.reason)}` : text(p.reason) || undefined,
        tone: p.verdict === "PASSED" ? "good" : "bad",
      };
    }
    case "verification.finished":
      return { kind: "test", title: `Tests ${text(p.verdict)}: ${count(p.passed)} of ${count(p.ran)} passed`, detail: text(p.reason) || undefined, tone: p.verdict === "PASSED" ? "good" : "bad" };
    case "verification.blocked":
      return { kind: "error", title: "Verification is unavailable for this stack, so nothing was generated", tone: "bad" };
    case "acceptance.finished": {
      const unmet = Array.isArray(p.criteria) ? (p.criteria as { code?: unknown; status?: unknown }[]).filter((c) => c.status !== "PASSED").map((c) => text(c.code)) : [];
      const label = p.kind === "browser" ? "Independent browser acceptance" : "Independent acceptance";
      return { kind: "test", title: `${label} ${text(p.verdict)}: ${count(p.passed)} of ${count(p.total)} criteria`, detail: unmet.length ? `Not passed: ${unmet.join(", ")}. ${text(p.reason)}` : text(p.reason) || undefined, tone: p.verdict === "PASSED" ? "good" : "bad" };
    }
    case "recovery.decided":
      return { kind: "recovery", title: `Recovery: ${text(p.category ?? p.failure_class)} → ${text(p.action)}`, detail: text(p.reason) || undefined, tone: p.action === "STOP" ? "bad" : "warn" };
    case "recovery.outcome":
      return { kind: "recovery", title: `Recovery ${p.resolved ? "resolved the failure" : "did not resolve the failure"}`, tone: p.resolved ? "good" : "warn" };
    case "delivery.finished":
      return { kind: "delivery", title: `Delivered ${count(p.files)} file${count(p.files) === 1 ? "" : "s"} into the project`, tone: "good" };
    case "analysis.gap_recorded":
      return {
        kind: "milestone",
        title: `Existing code compared with the requirements: ${count(p.missing_features)} to build, ${count(p.similarities)} already there`,
        detail: `${count(p.files)} file decisions, ${count(p.tests)} planned tests`,
        tone: "good",
      };
    case "github.publish_started":
      return { kind: "delivery", title: `Publishing ${count(p.files)} verified file${count(p.files) === 1 ? "" : "s"} to GitHub`, detail: text(p.repository) || undefined, tone: "neutral" };
    case "github.published":
      return p.pull_request_url
        ? { kind: "delivery", title: `Pull request opened on ${text(p.repository)}`, detail: text(p.pull_request_url), tone: "good" }
        : { kind: "delivery", title: `Repository ${text(p.repository)} created with the verified project`, detail: text(p.repository_url) || undefined, tone: "good" };
    case "rag.indexed":
      return { kind: "milestone", title: `Documents indexed: ${count(p.documents)} documents, ${count(p.chunks)} chunks`, detail: p.simulated ? "Test-double embedder: lexical, not semantic" : `Embedder ${text(p.embedder)}`, tone: p.simulated ? "warn" : "good" };
    case "drive.authorized":
      return { kind: "info", title: p.status === "pending_consent" ? "Google Drive permission needed" : "Google Drive access granted", tone: p.status === "pending_consent" ? "warn" : "good" };
    case "drive.consent_completed":
      return { kind: "info", title: "Google Drive access granted", tone: "good" };
    case "n8n.triggered":
      return { kind: "tool", title: `n8n workflow ${text(p.workflow)} started`, tone: "neutral" };
    case "n8n.callback":
      return { kind: "tool", title: `n8n workflow reported ${text(p.status)}`, tone: p.status === "success" ? "good" : "warn" };
    case "n8n.waiting":
      return { kind: "info", title: `Waiting for n8n's result of ${text(p.workflow)}`, detail: "The run continues by itself when n8n reports back", tone: "neutral" };
    case "n8n.resumed_run":
      return { kind: "state", title: `n8n reported back: the run continues`, tone: "good" };
    case "n8n.resume_refused":
      return { kind: "info", title: "n8n reported back; start the run again to continue", detail: text(p.reason) || undefined, tone: "warn" };
    case "repository.searched":
      return { kind: "tool", title: "Searched public repositories", tone: "neutral" };
    case "repository.imported":
      return { kind: "tool", title: `Imported code from ${text(p.full_name, "a repository")}`, detail: p.commit ? `Commit ${text(p.commit).slice(0, 12)}` : undefined, tone: "neutral" };
    case "approval.requested":
      return { kind: "info", title: `Waiting for approval: ${text(p.tool, "a decision")}`, tone: "warn" };
    case "approval.decided":
      return { kind: "info", title: `Approval ${text(p.decision ?? p.status, "decided")}`, tone: "neutral" };
    case "llm.error":
      return { kind: "error", title: `OpenAI error (${text(p.kind)})`, detail: text(p.message) || undefined, tone: "bad" };
    case "llm.stopped":
      return { kind: "info", title: "Stopped on purpose before another model request", detail: text(p.message) || undefined, tone: "warn" };
    case "run.paused":
      return { kind: "info", title: `Paused: ${text(p.reason)}`, detail: text(p.detail) || undefined, tone: "warn" };
    case "run.resumed":
      return { kind: "state", title: "Resumed", tone: "neutral" };
    case "run.failed":
      return { kind: "error", title: `Run failed (${text(p.reason, "error")})`, detail: text(p.detail) || undefined, tone: "bad" };
    case "run.cancelled":
      return { kind: "state", title: "Run cancelled", tone: "warn" };
    case "run.stopped":
      return { kind: "error", title: `Run stopped (${text(p.reason, "limit")})`, detail: text(p.detail) || undefined, tone: "bad" };
    case "run.completed":
      return { kind: "state", title: "Run completed", tone: "good" };
    default:
      return { kind: "info", title: event.type, tone: "neutral" };
  }
}

/** The run's events as a short, readable history. */
export function buildTimeline(events: RunEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let tools: { first: RunEvent; counts: Map<string, number> } | null = null;
  let edits: { first: RunEvent; paths: string[] } | null = null;

  const flush = () => {
    if (tools) {
      const summary = [...tools.counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name)).join(", ");
      items.push({ key: `tools-${tools.first.seq}`, kind: "coding", title: `Coding engine used: ${summary}`, ts: tools.first.ts, tone: "neutral" });
      tools = null;
    }
    if (edits) {
      items.push({ key: `edits-${edits.first.seq}`, kind: "files", title: `Edited ${edits.paths.join(", ")}`, ts: edits.first.ts, tone: "neutral" });
      edits = null;
    }
  };

  for (const event of events) {
    if (event.type === "coding.tool") {
      if (event.payload.status !== "completed") continue; // one line per finished call, not per status change
      tools ??= { first: event, counts: new Map() };
      const name = text(event.payload.tool, "tool");
      tools.counts.set(name, (tools.counts.get(name) ?? 0) + 1);
      continue;
    }
    if (event.type === "coding.file_edited") {
      edits ??= { first: event, paths: [] };
      const path = text(event.payload.path);
      if (path && !edits.paths.includes(path)) edits.paths.push(path);
      continue;
    }
    if (HIDDEN.has(event.type)) continue;
    flush();
    const item = describe(event);
    if (!item) continue;
    const previous = items[items.length - 1];
    if (previous && previous.title === item.title && previous.kind === item.kind) continue; // a repeat says nothing new
    items.push({ key: `e-${event.seq}`, ts: event.ts, ...item });
  }
  flush();
  return items;
}
