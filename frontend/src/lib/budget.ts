import type { Run } from "./types";

export interface RecordedSpend {
  total: number; // USD, over the runs whose cost is known
  counted: number; // runs with a known cost
  unknown: number; // runs whose cost could not be priced
}

/** What the backend's own runs record as spent. Runs whose cost is unknown are counted, not guessed. */
export function recordedSpend(runs: Run[]): RecordedSpend {
  const known = runs.filter((r) => r.cost_known);
  return { total: known.reduce((sum, r) => sum + r.cost_usd, 0), counted: known.length, unknown: runs.length - known.length };
}
