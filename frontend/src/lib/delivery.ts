import type { Report, Run } from "./types";

// Whether a finished run may be called a success. Only the backend's own record decides: the run reached COMPLETED
// and the evaluator's independent acceptance suite passed. Code that was generated, or tests the coding engine wrote
// for itself, are not enough.

export type DeliveryVerdict =
  | { kind: "success"; title: string }
  | { kind: "unverified"; title: string; reason: string }
  | { kind: "failed"; title: string; reason: string }
  | { kind: "in-progress"; title: string };

export function deliveryVerdict(run: Run, report: Report | null): DeliveryVerdict {
  if (run.state !== "COMPLETED") {
    if (run.state === "FAILED" || run.state === "STOPPED_BY_POLICY") return { kind: "failed", title: "The build did not produce a verified application", reason: run.status_detail ?? run.outcome ?? run.state };
    if (run.state === "CANCELLED") return { kind: "failed", title: "The run was cancelled", reason: run.status_detail ?? "Nothing was delivered." };
    return { kind: "in-progress", title: "The run has not finished" };
  }
  if (!report) return { kind: "unverified", title: "Completed, but the final report is not available", reason: "Without the report, independent acceptance cannot be confirmed." };
  const acceptance = report.acceptance;
  if (!acceptance?.configured) return { kind: "unverified", title: "Completed without independent verification", reason: acceptance?.note ?? "No independent acceptance suite was configured for this project, so only the coding engine's own tests ran." };
  if (acceptance.verdict !== "PASSED") return { kind: "failed", title: "Completed, but independent acceptance did not pass", reason: acceptance.reason ?? `Verdict: ${acceptance.verdict ?? "unknown"}` };
  if (report.provenance?.simulated?.length) return { kind: "unverified", title: "Completed in a simulated run", reason: `Simulated: ${report.provenance.simulated.join(", ")}` };
  return { kind: "success", title: "Application generated successfully" };
}

export function testResult(report: Report | null): string {
  if (!report) return "—";
  const acceptance = report.acceptance;
  if (acceptance?.configured && acceptance.verdict) return `Acceptance ${acceptance.verdict}`;
  const last = report.verifications?.at(-1);
  return last ? `Tests ${last.verdict} (${last.passed}/${last.ran})` : "Not run";
}
