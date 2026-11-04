export const MIN_PROMPT = 15;
export const MAX_PROMPT = 19_000; // the backend takes 20,000 characters, and the preferences need room

// Only Python command-line applications can be built and verified today (the backend's capabilities say so). A request
// for another kind is refused up front with one sentence, never quietly turned into something else.
const OTHER_KINDS =
  /\b(react|vue|angular|svelte|next\.?js|fastapi|flask|django|express|node\.?js|website|web\s?site|web\s?app(lication)?|web\s?page|landing page|frontend|front-end|mobile app|android|ios|swift|kotlin|flutter)\b/i;

/** A brief reason this request cannot be built yet, or null when it can be treated as a Python command-line application. */
export function unsupportedRequest(prompt: string): string | null {
  const match = OTHER_KINDS.exec(prompt);
  return match ? `“${match[0]}” applications cannot be built yet. DevPilot currently builds and verifies Python command-line applications.` : null;
}

/** Why the prompt cannot be sent yet, or null when it can. */
export function validatePrompt(prompt: string): string | null {
  const text = prompt.trim();
  if (!text) return "Describe the application you want to build.";
  if (text.length < MIN_PROMPT) return `Add a little more detail: at least ${MIN_PROMPT} characters.`;
  if (text.length > MAX_PROMPT) return `That is ${text.length.toLocaleString()} characters; the limit is ${MAX_PROMPT.toLocaleString()}.`;
  return null;
}
