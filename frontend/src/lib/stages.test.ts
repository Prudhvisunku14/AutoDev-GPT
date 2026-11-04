import { describe, expect, it } from "vitest";
import { ev, makeRun, plannedEvents } from "../test/fixtures";
import { deriveStages, type StageKey } from "./stages";

function statuses(view: ReturnType<typeof deriveStages>): Record<StageKey, string> {
  return Object.fromEntries(view.stages.map((s) => [s.key, s.status])) as Record<StageKey, string>;
}

function stage(view: ReturnType<typeof deriveStages>, key: StageKey) {
  const found = view.stages.find((s) => s.key === key);
  if (!found) throw new Error(`no stage ${key}`);
  return found;
}

describe("deriveStages", () => {
  it("shows every stage pending without a run", () => {
    const view = deriveStages(null, []);
    expect(Object.values(statuses(view))).toEqual(["pending", "pending", "pending", "pending", "pending"]);
    expect(view.finished).toBe(0);
    expect(view.active).toBe(false);
  });

  it("marks Analyze running while the requirement is analysed", () => {
    const view = deriveStages(makeRun({ state: "ANALYZING" }), [ev("run.state_changed", { from: "CREATED", to: "ANALYZING" })]);
    expect(statuses(view)).toEqual({ analyze: "running", retrieve: "pending", generate: "pending", test: "pending", deliver: "pending" });
    expect(stage(view, "analyze").summary).toBe("Understanding the requirements");
    expect(view.active).toBe(true);
  });

  it("skips Retrieve when planning used no retrieval, and times Analyze from real state changes", () => {
    const view = deriveStages(makeRun({ state: "IMPLEMENTING" }), plannedEvents());
    expect(statuses(view)).toEqual({ analyze: "completed", retrieve: "skipped", generate: "running", test: "pending", deliver: "pending" });
    expect(stage(view, "retrieve").summary).toBe("Not used in this run");
    expect(stage(view, "analyze").summary).toBe("Requirements understood: 4 requirements, 2 tasks planned on python-cli");
    expect(stage(view, "analyze").elapsedMs).toBe(20_000);
    expect(view.finished).toBe(2);
  });

  it("marks Retrieve completed when the run recorded a retrieval", () => {
    const events = [...plannedEvents(), ev("repository.searched", { query: "attendance" }, 8)];
    const view = deriveStages(makeRun({ state: "IMPLEMENTING" }), events);
    expect(stage(view, "retrieve").status).toBe("completed");
    expect(stage(view, "retrieve").summary).toBe("Searched public repositories");
  });

  it("marks every stage done when the run completed and delivered", () => {
    const events = [
      ...plannedEvents(),
      ev("coding.finished", { task_order: 1, status: "COMPLETED", kept: true, changes: [{ path: "a.py" }, { path: "test_a.py" }] }, 60),
      ev("verification.finished", { verdict: "PASSED", passed: 6, ran: 6 }, 80),
      ev("delivery.finished", { files: ["a.py", "test_a.py"] }, 90),
      ev("run.completed", {}, 91),
    ];
    const view = deriveStages(makeRun({ state: "COMPLETED", ended_at: "2026-11-01T10:01:31" }), events);
    expect(statuses(view)).toEqual({ analyze: "completed", retrieve: "skipped", generate: "completed", test: "completed", deliver: "completed" });
    expect(stage(view, "generate").summary).toBe("1 coding attempt kept, 2 files changed");
    expect(stage(view, "test").summary).toBe("6 of 6 tests passed");
    expect(stage(view, "deliver").summary).toBe("2 verified files delivered");
    expect(view.finished).toBe(5);
    expect(view.ended).toBe("COMPLETED");
  });

  it("puts a failure on the stage the run was in, and leaves later stages not reached", () => {
    const events = [...plannedEvents(), ev("run.failed", { previous_state: "IMPLEMENTING", reason: "error" }, 40)];
    const run = makeRun({ state: "FAILED", outcome: "FAILED", status_detail: "The coding engine stopped: rate limited" });
    const view = deriveStages(run, events);
    expect(statuses(view)).toEqual({ analyze: "completed", retrieve: "skipped", generate: "failed", test: "pending", deliver: "pending" });
    expect(stage(view, "generate").summary).toBe("The coding engine stopped: rate limited");
    expect(stage(view, "test").summary).toBe("Not reached");
    expect(stage(view, "deliver").summary).toBe("Not reached");
    expect(view.active).toBe(false);
  });

  it("falls back to the outcome when a failed run has no detail", () => {
    const view = deriveStages(makeRun({ state: "FAILED", outcome: "ERROR" }), [ev("run.failed", { previous_state: "ANALYZING" })]);
    expect(stage(view, "analyze").status).toBe("failed");
    expect(stage(view, "analyze").summary).toBe("The run ended here (ERROR)");
  });

  it("marks the stage a cancelled run was in as cancelled", () => {
    const events = [...plannedEvents(), ev("run.state_changed", { from: "IMPLEMENTING", to: "VERIFYING" }, 50), ev("run.cancelled", { previous_state: "VERIFYING" }, 55)];
    const view = deriveStages(makeRun({ state: "CANCELLED" }), events);
    expect(stage(view, "generate").status).toBe("completed");
    expect(stage(view, "test").status).toBe("cancelled");
    expect(stage(view, "deliver").summary).toBe("Not reached");
  });

  it("marks Generate and Test blocked when the stack has no verification runtime", () => {
    const events = [
      ...plannedEvents(),
      ev("verification.blocked", { components: [{ reason: "devpilot/verify-fastapi:1 is not built" }] }, 21),
      ev("run.failed", { previous_state: "IMPLEMENTING" }, 22),
    ];
    const view = deriveStages(makeRun({ state: "FAILED" }), events);
    expect(stage(view, "generate").status).toBe("blocked");
    expect(stage(view, "test").status).toBe("blocked");
    expect(stage(view, "test").summary).toBe("devpilot/verify-fastapi:1 is not built");
  });

  it("shows a run waiting for a person as waiting, not running", () => {
    const run = makeRun({ state: "AWAITING_APPROVAL", resume_state: "PLANNING" });
    const view = deriveStages(run, [ev("run.state_changed", { from: "CREATED", to: "ANALYZING" })]);
    expect(stage(view, "analyze").status).toBe("waiting");
    expect(stage(view, "analyze").summary).toBe("Waiting for a person's decision");
    expect(view.active).toBe(false);
  });

  it("shows a paused run as paused, with the reason", () => {
    const run = makeRun({ state: "IMPLEMENTING", pause_reason: "budget", status_detail: "The run budget is used up" });
    const view = deriveStages(run, plannedEvents());
    expect(stage(view, "generate").status).toBe("paused");
    expect(stage(view, "generate").summary).toBe("Paused: The run budget is used up");
    expect(view.active).toBe(false);
  });

  it("counts only finished stages as progress", () => {
    const view = deriveStages(makeRun({ state: "VERIFYING" }), [...plannedEvents(), ev("run.state_changed", { from: "IMPLEMENTING", to: "VERIFYING" }, 50)]);
    expect(view.finished).toBe(3); // analyze, retrieve (skipped), generate
    expect(view.total).toBe(5);
  });
});
