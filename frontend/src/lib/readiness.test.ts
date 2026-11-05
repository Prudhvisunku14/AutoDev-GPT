import { describe, expect, it } from "vitest";
import { DEFAULT_BUDGET, DEFAULT_CAPABILITIES } from "../test/render";
import { assessReadiness } from "./readiness";

const STATUS = { status: "ok" as const, version: "1", phase: "t", database: "ok" as const, workspaces_writable: true, egress_components: [], projects: 0, runs: 0 };
const ok = { capabilities: DEFAULT_CAPABILITIES, budget: DEFAULT_BUDGET, status: STATUS, stackId: "python-cli" };

describe("assessReadiness", () => {
  it("is ready only when every backend answer allows a build", () => {
    expect(assessReadiness(ok)).toEqual({ kind: "ready", ready: true, summary: "Ready to build", details: [] });
  });

  it("is checking, not ready, until the backend has answered", () => {
    expect(assessReadiness({ stackId: "python-cli" })).toMatchObject({ kind: "checking", ready: false });
  });

  it.each([
    ["coding", { coding: { ready: false, label: "x", detail: "OPENCODE_ENABLED is false", simulated: false } }, "Setup required — Coding engine is not configured"],
    ["gpt", { gpt: { ready: false, label: "x", detail: "OPENAI_API_KEY is not set", simulated: false } }, "Setup required — OpenAI is not configured"],
    ["sandbox", { sandbox: { ready: false, label: "x", detail: "no sandbox", simulated: false } }, "Setup required — Docker sandbox is not available"],
    ["prices", { prices: { ready: false, label: "x", detail: "prices not set", simulated: false } }, "Setup required — spending prices are not set"],
  ])("a %s that is not ready blocks, with one short line and the detail kept for advanced use", (_name, part, summary) => {
    const result = assessReadiness({ ...ok, capabilities: { ...DEFAULT_CAPABILITIES, ...part, can_build: false } });
    expect(result).toMatchObject({ ready: false, kind: "setup", summary });
    expect(result.summary).not.toMatch(/[A-Z]{3,}_[A-Z_]+/); // no environment names in the line people read
    expect(result.details.length).toBeGreaterThan(0);
  });

  it("blocks when the stack cannot be verified, when prices are unknown, or when the backend is unhealthy or unreachable", () => {
    expect(assessReadiness({ ...ok, stackId: "python-fastapi" }).summary).toBe("Setup required — Python web API with FastAPI cannot be verified yet");
    expect(assessReadiness({ ...ok, budget: { ...DEFAULT_BUDGET, cost_known: false } }).summary).toBe("Setup required — spending prices are not set");
    expect(assessReadiness({ ...ok, status: { ...STATUS, status: "degraded" } }).summary).toBe("Setup required — the backend is not healthy");
    expect(assessReadiness({ ...ok, failed: { status: new Error("The DevPilot backend could not be reached. Is it running?") } })).toMatchObject({
      ready: false,
      summary: "Setup required — the backend is not reachable",
      details: ["The DevPilot backend could not be reached. Is it running?"],
    });
  });
});

describe("assessReadiness when GPT chooses the stack", () => {
  it("is ready when at least one stack is verifiable, whatever the request says", () => {
    expect(assessReadiness({ ...ok, stackId: null })).toMatchObject({ kind: "ready", ready: true });
  });

  it("blocks with every stack's reason when none can be verified", () => {
    const none = { ...DEFAULT_CAPABILITIES, stacks: DEFAULT_CAPABILITIES.stacks.map((s) => ({ ...s, verifiable: false, reason: `${s.id} runtime is not approved` })) };
    const result = assessReadiness({ ...ok, capabilities: none, stackId: null });
    expect(result).toMatchObject({ ready: false, summary: "Setup required — no kind of application can be verified yet" });
    expect(result.details.join(" ")).toContain("python-cli runtime is not approved");
  });
});
