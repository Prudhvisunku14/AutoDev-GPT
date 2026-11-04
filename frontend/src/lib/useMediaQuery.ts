import { useCallback, useSyncExternalStore } from "react";

/**
 * Whether a media query matches now, updated when it changes. Without matchMedia (tests), `fallback`.
 * `subscribe` is stable per query: a new function on every render would make React remove and re-add the listener each time.
 */
export function useMediaQuery(query: string, fallback = true): boolean {
  const supported = typeof window !== "undefined" && typeof window.matchMedia === "function";
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (!supported) return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query, supported],
  );
  const snapshot = useCallback(() => (supported ? window.matchMedia(query).matches : fallback), [query, supported, fallback]);
  return useSyncExternalStore(subscribe, snapshot, () => fallback);
}
