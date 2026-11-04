import { describe, expect, it } from "vitest";
import { makeReport, makeRun } from "../test/fixtures";
import { deliveryVerdict, testResult } from "./delivery";
import { phaseMessage } from "./phase";

const completed = makeRun({ state: "COMPLETED", outcome: "SUCCESS" });

describe("deliveryVerdict", () => {
  it("claims success only for a completed run whose independent acceptance passed", () => {
    expect(deliveryVerdict(completed, makeReport())).toEqual({ kind: "success", title: "Application generated successfully" });
  });

  it("does not claim success without independent acceptance", () => {
    const verdict = deliveryVerdict(completed, makeReport({ acceptance: { configured: false, note: "No suite attached" } }));
    expect(verdict.kind).toBe("unverified");
    expect(verdict.title).toBe("Completed without independent verification");
  });

  it("calls a completed run with failed acceptance a failure", () => {
    const verdict = deliveryVerdict(completed, makeReport({ acceptance: { configured: true, verdict: "FAILED", passed: 3, total: 5, reason: "2 MUST criteria failed" } }));
    expect(verdict).toMatchObject({ kind: "failed", reason: "2 MUST criteria failed" });
  });

  it("does not claim success for a simulated run", () => {
    const report = makeReport({ provenance: { gpt: "simulated", coding_providers: [], sandbox: "local", simulated: ["gpt"], fully_real: false } });
    expect(deliveryVerdict(completed, report).kind).toBe("unverified");
  });

  it("does not claim success when the report is missing", () => {
    expect(deliveryVerdict(completed, null).kind).toBe("unverified");
  });

  it("reports a failed run with the backend's own detail", () => {
    const verdict = deliveryVerdict(makeRun({ state: "FAILED", status_detail: "Verification is unavailable for this stack" }), null);
    expect(verdict).toMatchObject({ kind: "failed", reason: "Verification is unavailable for this stack" });
  });

  it("says nothing final while the run is working", () => {
    expect(deliveryVerdict(makeRun({ state: "VERIFYING" }), null).kind).toBe("in-progress");
  });
});

describe("testResult", () => {
  it("prefers the acceptance verdict, then the engine's last test run", () => {
    expect(testResult(makeReport())).toBe("Acceptance PASSED");
    expect(testResult(makeReport({ acceptance: { configured: false } }))).toBe("Tests PASSED (6/6)");
    expect(testResult(makeReport({ acceptance: { configured: false }, verifications: [] }))).toBe("Not run");
    expect(testResult(null)).toBe("—");
  });
});

describe("phaseMessage", () => {
  it("uses the backend's detail for a failed run", () => {
    expect(phaseMessage(makeRun({ state: "FAILED", status_detail: "Rate limited" }))).toEqual({ tone: "error", title: "Failed", text: "Rate limited" });
  });

  it("says a paused run is paused, whatever its state", () => {
    expect(phaseMessage(makeRun({ state: "IMPLEMENTING", pause_reason: "budget" })).title).toBe("Paused");
  });

  it("explains each working phase", () => {
    expect(phaseMessage(makeRun({ state: "VERIFYING" })).text).toBe("The tests are running in the Docker sandbox.");
    expect(phaseMessage(makeRun({ state: "AWAITING_APPROVAL" })).tone).toBe("warn");
  });
});
