import { describe, expect, it } from "vitest";
import { ev } from "../test/fixtures";
import { buildTimeline } from "./timeline";

describe("buildTimeline", () => {
  it("drops bookkeeping events", () => {
    const items = buildTimeline([ev("llm.request"), ev("tool.called", { tool: "x" }), ev("tool.started"), ev("run.created"), ev("run.started")]);
    expect(items.map((i) => i.title)).toEqual(["Run started"]);
  });

  it("folds the coding engine's tool calls into one line with counts", () => {
    const items = buildTimeline([
      ev("coding.tool", { tool: "apply_patch", status: "running" }),
      ev("coding.tool", { tool: "apply_patch", status: "completed" }),
      ev("coding.tool", { tool: "read", status: "completed" }),
      ev("coding.tool", { tool: "apply_patch", status: "completed" }),
      ev("verification.started"),
    ]);
    expect(items.map((i) => i.title)).toEqual(["Coding engine used: apply_patch ×2, read", "Tests started in the sandbox"]);
  });

  it("folds file edits into one line, each path once", () => {
    const items = buildTimeline([ev("coding.file_edited", { path: "a.py" }), ev("coding.file_edited", { path: "b.py" }), ev("coding.file_edited", { path: "a.py" })]);
    expect(items).toHaveLength(1);
    expect(items[0]?.title).toBe("Edited a.py, b.py");
    expect(items[0]?.kind).toBe("files");
  });

  it("drops a line that repeats the one before it", () => {
    const items = buildTimeline([ev("run.state_changed", { to: "IMPLEMENTING" }), ev("run.state_changed", { to: "IMPLEMENTING" })]);
    expect(items).toHaveLength(1);
  });

  it("describes tests, recovery and failures with the right tone", () => {
    const items = buildTimeline([
      ev("verification.finished", { verdict: "FAILED", passed: 4, ran: 6, reason: "2 tests failed" }),
      ev("recovery.decided", { category: "TEST_FAILURE", action: "REPAIR", reason: "a test failed" }),
      ev("verification.finished", { verdict: "PASSED", passed: 6, ran: 6 }),
      ev("run.failed", { reason: "budget", detail: "The run budget is used up" }),
    ]);
    expect(items.map((i) => [i.title, i.tone])).toEqual([
      ["Tests FAILED: 4 of 6 passed", "bad"],
      ["Recovery: TEST_FAILURE → REPAIR", "warn"],
      ["Tests PASSED: 6 of 6 passed", "good"],
      ["Run failed (budget)", "bad"],
    ]);
    expect(items[0]?.detail).toBe("2 tests failed");
  });

  it("shows events it does not know by their type rather than hiding them", () => {
    expect(buildTimeline([ev("something.new")]).map((i) => i.title)).toEqual(["something.new"]);
  });
});
