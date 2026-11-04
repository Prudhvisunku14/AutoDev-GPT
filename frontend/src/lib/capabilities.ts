// The backend capabilities endpoint is authoritative for current stack support.
// These helper types remain only for the older requirement-formatting helpers below.

export type TechId = "fastapi" | "react" | "sqlite";
export type Support = "supported" | "plan-only";

export interface Tech {
  id: TechId;
  label: string;
  category: "Backend" | "Frontend" | "Database";
  preference: string; // the sentence added to the requirement
  support: Support;
  note: string;
}

export const TECHS: readonly Tech[] = [
  {
    id: "fastapi",
    label: "FastAPI",
    category: "Backend",
    preference: "Backend: Python with FastAPI.",
    support: "plan-only",
    note: "FastAPI can be planned, but the backend's FastAPI test runtime (devpilot/verify-fastapi:1) is not built or approved yet, so a FastAPI build stops before any code is written.",
  },
  {
    id: "react",
    label: "React",
    category: "Frontend",
    preference: "Frontend: React with Vite.",
    support: "plan-only",
    note: "React can be planned, but the backend's React test runtime (devpilot/verify-react:1) is not built or approved yet, so a React build stops before any code is written.",
  },
  {
    id: "sqlite",
    label: "SQLite",
    category: "Database",
    preference: "Database: SQLite (Python's built-in sqlite3).",
    support: "supported",
    note: "SQLite comes with Python's standard library, which the approved Python test runtime has.",
  },
];

/** The stack every build uses for now: the one the backend has verified end to end. */
export const DEFAULT_STACK = "python-cli";

export const CAPABILITY_SOURCE = "Stack support is read from the authenticated backend capabilities endpoint.";

export function techById(id: TechId): Tech {
  const tech = TECHS.find((t) => t.id === id);
  if (!tech) throw new Error(`unknown technology ${id}`);
  return tech;
}

/** The requirement as the backend receives it: the user's words, then the chosen technologies as preferences. */
export function requirementWithPreferences(prompt: string, selected: readonly TechId[]): string {
  const text = prompt.trim();
  if (!selected.length) return text;
  const lines = TECHS.filter((t) => selected.includes(t.id)).map((t) => `- ${t.preference}`);
  return `${text}\n\nTechnology preferences:\n${lines.join("\n")}`;
}

export function requirementForStack(prompt: string, stack: { id: string; name: string }): string {
  return `${prompt.trim()}\n\nTechnology stack selected by the user: ${stack.name} (stack id: ${stack.id}). Use this stack exactly.`;
}

export function capabilityWarnings(selected: readonly TechId[]): Tech[] {
  return TECHS.filter((t) => selected.includes(t.id) && t.support !== "supported");
}

const NAME_LIMIT = 60;

/** A short project name from the prompt's first words. The backend accepts up to 200 characters. */
export function projectNameFrom(prompt: string): string {
  const words = prompt.trim().split(/\s+/).join(" ");
  const sentence = words.split(/(?<=[.!?])\s/)[0] ?? words;
  return sentence.length <= NAME_LIMIT ? sentence.replace(/[.!?]$/, "") : `${sentence.slice(0, NAME_LIMIT - 1).trimEnd()}…`;
}
