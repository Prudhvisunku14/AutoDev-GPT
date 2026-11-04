// Test data only. Production code never imports this file.
import type { Project, Report, Run, RunEvent } from "../lib/types";

export const T0 = Date.parse("2026-11-01T10:00:00Z");

export function at(seconds: number): string {
  return new Date(T0 + seconds * 1000).toISOString();
}

export function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "p1",
    name: "Attendance tracker",
    requirement_text: "Create a Python student attendance tracker with unit tests.",
    stack: null,
    status: "ACTIVE",
    created_at: "2026-11-01T09:00:00",
    has_acceptance_suite: false,
    ...overrides,
  };
}

export function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: "r1-0000-aaaa",
    project_id: "p1",
    state: "CREATED",
    resume_state: null,
    turns: 0,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: 0,
    created_at: "2026-11-01T10:00:00",
    started_at: null,
    ended_at: null,
    outcome: null,
    tool_calls: 0,
    cost_known: true,
    active_seconds: 0,
    pause_reason: null,
    status_detail: null,
    ...overrides,
  };
}

let seq = 0;

export function ev(type: string, payload: Record<string, unknown> = {}, seconds = 0): RunEvent {
  seq += 1;
  return { seq, type, project_id: "p1", run_id: "r1-0000-aaaa", payload, ts: at(seconds) };
}

/** The state changes of a run that went through planning and into coding. */
export function plannedEvents(): RunEvent[] {
  return [
    ev("run.started", {}, 0),
    ev("run.state_changed", { from: "CREATED", to: "ANALYZING" }, 0),
    ev("analysis.accepted", { requirements: 4 }, 5),
    ev("run.state_changed", { from: "ANALYZING", to: "PLANNING" }, 5),
    ev("plan.accepted", { tasks: 2, stack: "python-cli" }, 12),
    ev("run.state_changed", { from: "PLANNING", to: "DEFINING_CRITERIA" }, 12),
    ev("run.state_changed", { from: "DEFINING_CRITERIA", to: "IMPLEMENTING" }, 20),
  ];
}

export function makeReport(overrides: Partial<Report> = {}): Report {
  return {
    project: { id: "p1", name: "Attendance tracker", requirement: "…", stack: "python-cli" },
    run: { id: "r1-0000-aaaa", state: "COMPLETED", outcome: "SUCCESS", detail: null, turns: 9, tokens: 12000, cost_usd: 0.05, repairs: 0 },
    tasks: [],
    verifications: [{ verdict: "PASSED", passed: 6, ran: 6 }],
    acceptance: { configured: true, verdict: "PASSED", passed: 5, total: 5 },
    delivered_files: [{ path: "attendance.py", change: "added", bytes: 1200 }],
    real_workspace_changed: true,
    provenance: { gpt: "real", coding_providers: ["opencode"], sandbox: "docker", simulated: [], fully_real: true },
    ...overrides,
  };
}
