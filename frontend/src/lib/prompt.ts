import type { StackCapability } from "./types";

export const MIN_PROMPT = 15;
export const MAX_PROMPT = 19_000; // the backend takes 20,000 characters, and the preferences need room

// What kind of application the request asks for. This only proposes a stack: the backend's capabilities decide whether
// it can be built, and the approval dialog names the stack before anything starts, so a wrong guess is visible.
// Explicit command-line wording wins over incidental words ("a CLI that downloads a web page" stays a CLI).
const MOBILE = /\b(mobile app|android|ios app|iphone app|swift|kotlin|flutter|react native)\b/i;
const CLI = /\b(command[- ]line|cli|terminal (app|application|tool)|console (app|application)|python script)\b/i;
const REACT = /\b(react|vue|angular|svelte|next\.?js|single[- ]page app(lication)?|spa)\b/i;
const API = /\b(fastapi|flask|django|express|rest api|web api|backend api|http api)\b/i;
const SITE = /\b(website|web\s?site|web\s?page|landing page|home\s?page|portfolio site|static site|html page|web\s?app(lication)?|browser game)\b/i;

export const DEFAULT_STACK_ID = "python-cli";

export interface StackChoice {
  /** The backend stack the request maps to, or null when no stack can build this kind of application. */
  stackId: string | null;
  /** The words that decided it, for the explanation. */
  matched: string | null;
}

/** The stack this request asks for, from its wording alone. */
export function requestedStack(prompt: string): StackChoice {
  const pick = (pattern: RegExp, stackId: string | null): StackChoice | null => {
    const match = pattern.exec(prompt);
    return match ? { stackId, matched: match[0] } : null;
  };
  return (
    pick(MOBILE, null) ??
    pick(CLI, "python-cli") ??
    pick(REACT, "react-vite") ??
    pick(API, "python-fastapi") ??
    pick(SITE, "static-html") ?? { stackId: DEFAULT_STACK_ID, matched: null }
  );
}

export interface StackDecision {
  stack: StackCapability | null;
  /** One sentence when this request cannot be built on this backend; null when it can (or the backend is still loading). */
  refusal: string | null;
}

/**
 * The stack to build with, decided by the backend's own capabilities: the requested stack if the backend can verify it,
 * otherwise a refusal that quotes the backend's reason. Never a silent fallback to another kind of application.
 */
export function decideStack(prompt: string, stacks: readonly StackCapability[] | undefined): StackDecision {
  const choice = requestedStack(prompt);
  if (choice.stackId === null) {
    return { stack: null, refusal: `“${choice.matched}” applications cannot be built: DevPilot has no stack for mobile apps.` };
  }
  if (!stacks) return { stack: null, refusal: null }; // capabilities not loaded yet: decided again with fresh data at approval
  const stack = stacks.find((candidate) => candidate.id === choice.stackId);
  if (!stack) {
    return { stack: null, refusal: `This backend has no ${choice.stackId} stack, so “${choice.matched ?? "this request"}” cannot be built here.` };
  }
  if (!stack.verifiable) {
    return { stack, refusal: `${stack.name} builds are not available on this backend yet: ${stack.reason}.` };
  }
  return { stack, refusal: null };
}

/** Why the prompt cannot be sent yet, or null when it can. */
export function validatePrompt(prompt: string): string | null {
  const text = prompt.trim();
  if (!text) return "Describe the application you want to build.";
  if (text.length < MIN_PROMPT) return `Add a little more detail: at least ${MIN_PROMPT} characters.`;
  if (text.length > MAX_PROMPT) return `That is ${text.length.toLocaleString()} characters; the limit is ${MAX_PROMPT.toLocaleString()}.`;
  return null;
}
