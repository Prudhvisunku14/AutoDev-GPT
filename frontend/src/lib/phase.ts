import type { Run } from "./types";

export interface PhaseMessage {
  tone: "info" | "warn" | "error";
  title: string;
  text: string;
}

/** One sentence for what the run is doing or how it ended. The backend's own detail wins over the generic wording. */
export function phaseMessage(run: Run): PhaseMessage {
  const detail = run.status_detail ?? "";
  if (run.pause_reason) return { tone: "warn", title: "Paused", text: detail || run.pause_reason };
  switch (run.state) {
    case "CREATED":
      return { tone: "info", title: "Not started", text: "This run has not been started." };
    case "ANALYZING":
    case "PLANNING":
    case "DEFINING_CRITERIA":
      return { tone: "info", title: "Planning", text: "GPT is analysing the requirement, planning the tasks and defining how they will be tested." };
    case "IMPLEMENTING":
      return { tone: "info", title: "Coding", text: "The coding engine is writing files in a disposable copy of the project." };
    case "VERIFYING":
      return { tone: "info", title: "Testing", text: "The tests are running in the Docker sandbox." };
    case "REPAIRING":
      return { tone: "warn", title: "Repairing", text: "A test failed; the coding engine is repairing the code." };
    case "DOCUMENTING":
      return { tone: "info", title: "Delivering", text: "The verified files are being delivered into the project." };
    case "AWAITING_APPROVAL":
      return { tone: "warn", title: "Waiting for a decision", text: "The run is waiting for a person to approve a step or answer a question." };
    case "COMPLETED":
      return { tone: "info", title: "Completed", text: detail || "The run finished." };
    case "CANCELLED":
      return { tone: "warn", title: "Cancelled", text: detail || "The run was cancelled." };
    case "STOPPED_BY_POLICY":
      return { tone: "error", title: "Stopped by a limit or policy", text: detail || run.outcome || "A limit stopped the run." };
    case "FAILED":
      return { tone: "error", title: "Failed", text: detail || run.outcome || "The run failed." };
  }
}
