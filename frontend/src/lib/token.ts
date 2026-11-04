import { useSyncExternalStore } from "react";

// The backend's API token, typed in by the user: never in an env file, never in the bundle, never in a URL. By default it
// is remembered on this computer (localStorage) so a new tab or a browser restart does not ask again; the user can choose
// this tab only (sessionStorage) instead. A token the backend refuses is forgotten in both places (queries.ts), and
// "Disconnect" forgets it too.

const KEY = "devpilot-token";
const listeners = new Set<() => void>();

function storages(): Storage[] {
  const found: Storage[] = [];
  try {
    found.push(window.sessionStorage);
  } catch {
    // storage blocked
  }
  try {
    found.push(window.localStorage);
  } catch {
    // storage blocked
  }
  return found;
}

function read(): string | null {
  for (const storage of storages()) {
    try {
      const value = storage.getItem(KEY);
      if (value) return value;
    } catch {
      // ignore: the user is asked again
    }
  }
  return null;
}

export function getToken(): string | null {
  return read();
}

/** Whether the stored token is remembered across tabs and restarts. */
export function isRemembered(): boolean {
  try {
    return !!window.localStorage.getItem(KEY);
  } catch {
    return false;
  }
}

/** A pasted token without surrounding whitespace or quotes (`.env` lines are often copied with them). */
export function cleanToken(raw: string): string {
  return raw.trim().replace(/^(['"])(.*)\1$/, "$2").trim();
}

export function setToken(token: string, { remember = false }: { remember?: boolean } = {}): void {
  const value = cleanToken(token);
  try {
    window.sessionStorage.setItem(KEY, value);
  } catch {
    // nothing to do: the next request will be refused and the user asked again
  }
  try {
    if (remember) window.localStorage.setItem(KEY, value);
    else window.localStorage.removeItem(KEY);
  } catch {
    // remembering is best effort; this tab still has the token
  }
  listeners.forEach((listener) => listener());
}

export function clearToken(): void {
  for (const storage of storages()) {
    try {
      storage.removeItem(KEY);
    } catch {
      // ignore
    }
  }
  listeners.forEach((listener) => listener());
}

/** Runs `listener` after every change of the stored token (set or cleared). Returns the unsubscribe function. */
export function onTokenChange(listener: () => void): () => void {
  return subscribe(listener);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const fromOtherTab = (event: StorageEvent) => {
    if (event.key === KEY || event.key === null) listener(); // connecting or disconnecting in another tab applies here too
  };
  window.addEventListener("storage", fromOtherTab);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", fromOtherTab);
  };
}

export function useToken(): string | null {
  return useSyncExternalStore(subscribe, read, () => null);
}
