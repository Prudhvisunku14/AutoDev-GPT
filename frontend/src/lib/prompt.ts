import type { StackCapability } from "./types";

export const MIN_PROMPT = 15;
export const MAX_PROMPT = 19_000; // the backend takes 20,000 characters, and the preferences need room

// The application's stack is not guessed here from keywords. GPT analyses the request and chooses a stack from the backend's
// catalog while planning, and the backend's code checks that choice: a stack it cannot verify is never coded or delivered.
// The dashboard only shows which stacks this backend can verify, so the person knows what can be built before approving.

/** The stacks this backend can build and verify now, which GPT chooses from. */
export function verifiableStacks(stacks: readonly StackCapability[] | undefined): StackCapability[] {
  return (stacks ?? []).filter((stack) => stack.verifiable);
}

/** Why the prompt cannot be sent yet, or null when it can. */
export function validatePrompt(prompt: string): string | null {
  const text = prompt.trim();
  if (!text) return "Describe the application you want to build.";
  if (text.length < MIN_PROMPT) return `Add a little more detail: at least ${MIN_PROMPT} characters.`;
  if (text.length > MAX_PROMPT) return `That is ${text.length.toLocaleString()} characters; the limit is ${MAX_PROMPT.toLocaleString()}.`;
  return null;
}
