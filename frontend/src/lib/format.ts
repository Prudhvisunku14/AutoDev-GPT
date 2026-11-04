// Display helpers. Times come from the backend in UTC ISO form, without a zone suffix on SQLite rows.

export function parseTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value: string | null | undefined): string {
  const date = parseTime(value);
  return date ? date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseTime(value);
  return date ? date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

export function formatCost(run: { cost_usd: number; cost_known: boolean }): string {
  return run.cost_known ? `$${run.cost_usd.toFixed(4)}` : "Unknown";
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function stateLabel(state: string): string {
  return state.charAt(0) + state.slice(1).toLowerCase().replaceAll("_", " ");
}
