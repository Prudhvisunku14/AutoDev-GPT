import type { BackendStatus, Budget, Capabilities } from "./types";

// One question, asked the same way everywhere: may a build start now? The backend's own answers decide it
// (/capabilities, /budget, /status); nothing here is a guess. `summary` is the short line people see, `details` the
// full diagnostics for the optional advanced area. The backend repeats every one of these checks when a run starts.

export interface Readiness {
  kind: "checking" | "ready" | "setup";
  ready: boolean;
  summary: string;
  details: string[];
}

interface Input {
  capabilities?: Capabilities;
  budget?: Budget;
  status?: BackendStatus;
  stackId: string | null | undefined;
  failed?: { capabilities?: unknown; budget?: unknown; status?: unknown };
}

function text(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function assessReadiness({ capabilities, budget, status, stackId, failed = {} }: Input): Readiness {
  const details: string[] = [];
  let summary: string | null = null;
  const block = (short: string, detail?: string | null) => {
    summary ??= short;
    if (detail && !details.includes(detail)) details.push(detail);
  };

  if (failed.capabilities || failed.budget || failed.status) {
    block("Setup required — the backend is not reachable");
    for (const [error, fallback] of [[failed.capabilities, "Build capabilities could not be read."], [failed.budget, "The spending limit could not be read."], [failed.status, "Backend status could not be read."]] as const) {
      if (error) details.push(text(error, fallback));
    }
  } else if (!capabilities || !budget || !status) {
    return { kind: "checking", ready: false, summary: "Checking the backend…", details };
  }

  if (capabilities) {
    for (const [part, short] of [
      [capabilities.coding, "Setup required — Coding engine is not configured"],
      [capabilities.gpt, "Setup required — OpenAI is not configured"],
      [capabilities.sandbox, "Setup required — Docker sandbox is not available"],
      [capabilities.prices, "Setup required — spending prices are not set"],
    ] as const) {
      if (!part.ready) block(short, part.detail ?? short);
    }
    for (const problem of capabilities.problems) if (!details.some((existing) => existing.includes(problem) || problem.includes(existing))) details.push(problem);
    const stack = capabilities.stacks.find((candidate) => candidate.id === stackId);
    if (!stack?.verifiable) block("Setup required — Python applications cannot be verified yet", stack?.reason ?? "The Python stack is not available on this backend.");
    if (!capabilities.can_build && !summary) block("Setup required — the backend cannot build yet", "The backend reports that builds cannot start.");
  }
  if (budget && !budget.cost_known) block("Setup required — spending prices are not set", budget.estimate);
  if (status && status.status !== "ok") block("Setup required — the backend is not healthy", `Backend status is ${status.status}.`);

  return summary ? { kind: "setup", ready: false, summary, details } : { kind: "ready", ready: true, summary: "Ready to build", details };
}
