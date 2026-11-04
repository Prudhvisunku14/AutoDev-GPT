import { useSyncExternalStore } from "react";

// The backend's API token, typed in by the user. It lives only in this tab's sessionStorage, as the backend's own
// read-only dashboard keeps it: never in an env file, never in the bundle, never in a URL.

const KEY = "devpilot-token";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null; // storage blocked: the user is asked again
  }
}

export function getToken(): string | null {
  return read();
}

export function setToken(token: string): void {
  try {
    window.sessionStorage.setItem(KEY, token.trim());
  } catch {
    // nothing to do: the next request will be refused and the user asked again
  }
  listeners.forEach((listener) => listener());
}

export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useToken(): string | null {
  return useSyncExternalStore(subscribe, read, () => null);
}
