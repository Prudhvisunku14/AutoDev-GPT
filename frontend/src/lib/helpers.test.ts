import { describe, expect, it } from "vitest";
import { makeRun } from "../test/fixtures";
import { recordedSpend } from "./budget";
import { capabilityWarnings, projectNameFrom, requirementWithPreferences } from "./capabilities";
import { buildTree } from "./fileTree";
import { formatCost, formatDuration, parseTime } from "./format";
import { MAX_PROMPT, MIN_PROMPT, validatePrompt } from "./prompt";
import { ACTIVE_POLL_MS, pollFor, WAITING_POLL_MS } from "./queries";

describe("capabilities", () => {
  it("adds the chosen technologies to the requirement as preferences", () => {
    expect(requirementWithPreferences("  Build a tracker.  ", [])).toBe("Build a tracker.");
    expect(requirementWithPreferences("Build a tracker.", ["sqlite", "fastapi"])).toBe(
      "Build a tracker.\n\nTechnology preferences:\n- Backend: Python with FastAPI.\n- Database: SQLite (Python's built-in sqlite3).",
    );
  });

  it("warns about FastAPI and React, which the backend can plan but not yet verify", () => {
    expect(capabilityWarnings(["fastapi", "react", "sqlite"]).map((t) => t.id)).toEqual(["fastapi", "react"]);
    expect(capabilityWarnings(["sqlite"])).toEqual([]);
  });

  it("names a project from the prompt's first sentence", () => {
    expect(projectNameFrom("Create a habit tracker. It should have streaks.")).toBe("Create a habit tracker");
    const long = projectNameFrom("Create a very long application description that keeps going well past the sixty character limit");
    expect(long).toHaveLength(60);
    expect(long.endsWith("…")).toBe(true);
  });
});

describe("validatePrompt", () => {
  it("asks for a description, more detail, or less text", () => {
    expect(validatePrompt("   ")).toBe("Describe the application you want to build.");
    expect(validatePrompt("a todo app")).toContain(`at least ${MIN_PROMPT}`);
    expect(validatePrompt("x".repeat(MAX_PROMPT + 1))).toContain("the limit is");
    expect(validatePrompt("Build a todo list application with tests")).toBeNull();
  });
});

describe("buildTree", () => {
  it("lists folders before files, alphabetically", () => {
    const tree = buildTree(["z.py", "src/b.py", "src/a.py", "README.md"]);
    expect(tree.map((n) => n.name)).toEqual(["src", "README.md", "z.py"]);
    expect(tree[0]?.children.map((n) => n.path)).toEqual(["src/a.py", "src/b.py"]);
  });
});

describe("budget and formatting", () => {
  it("adds up only the costs the backend knows", () => {
    const spend = recordedSpend([makeRun({ cost_usd: 0.1, cost_known: true }), makeRun({ cost_usd: 0.25, cost_known: true }), makeRun({ cost_usd: 9, cost_known: false })]);
    expect(spend.total).toBeCloseTo(0.35);
    expect(spend).toMatchObject({ counted: 2, unknown: 1 });
  });

  it("formats costs, durations and zone-less UTC times", () => {
    expect(formatCost({ cost_usd: 0.0389, cost_known: true })).toBe("$0.0389");
    expect(formatCost({ cost_usd: 0, cost_known: false })).toBe("Unknown");
    expect(formatDuration(65_000)).toBe("1m 5s");
    expect(parseTime("2026-11-01T10:00:00")?.toISOString()).toBe("2026-11-01T10:00:00.000Z");
  });
});

describe("polling", () => {
  it("polls a working run fast, a waiting run slowly, and a finished or paused run not at all", () => {
    expect(pollFor(makeRun({ state: "IMPLEMENTING" }))).toBe(ACTIVE_POLL_MS);
    expect(pollFor(makeRun({ state: "AWAITING_APPROVAL" }))).toBe(WAITING_POLL_MS);
    expect(pollFor(makeRun({ state: "COMPLETED" }))).toBe(false);
    expect(pollFor(makeRun({ state: "IMPLEMENTING", pause_reason: "budget" }))).toBe(false);
    expect(pollFor(undefined)).toBe(false);
  });
});
