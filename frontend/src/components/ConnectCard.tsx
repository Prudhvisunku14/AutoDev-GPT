import { KeyRound, Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, UnauthorizedError } from "../lib/api";
import { useApi } from "../lib/apiContext";
import { SESSION, cleanToken, setToken } from "../lib/token";
import { Notice } from "./Notice";

/**
 * Signs the dashboard in. The usual way needs no typing: the launcher prints a one-time link (`#login=<code>`), and opening it
 * signs this browser in. Otherwise the person pastes the API token once; it goes to POST /session, which answers with an
 * HttpOnly session cookie, and the token itself is never kept by the browser. A wrong token is refused and this card says
 * why. "Remember" keeps the cookie after the browser closes; unticked, it ends with the browser session.
 */
export function ConnectCard() {
  const api = useApi();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remember, setRemember] = useState(true);
  const probed = useRef(false);

  useEffect(() => {
    if (probed.current) return; // once, even under React's double effects in development
    probed.current = true;
    const code = new URLSearchParams(window.location.hash.slice(1)).get("login");
    if (code) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search); // the code works once: never leave it in the address bar
      setBusy(true);
      api
        .exchangeLogin(code)
        .then(() => setToken(SESSION, { remember: true }))
        .catch((failure: unknown) => {
          setBusy(false);
          setError(failure instanceof Error ? failure.message : "The sign-in link did not work: open the newest one the launcher printed.");
        });
      return;
    }
    api
      .getSession()
      .then((session) => {
        if (session.authenticated) setToken(SESSION, { remember: true }); // already signed in by cookie, in another tab or before a restart
      })
      .catch(() => undefined); // an older backend without sessions: the token form below still works
  }, [api]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const token = cleanToken(value);
    if (!token) {
      setError("Paste the backend's API token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.signIn(token, remember);
    } catch (failure) {
      setBusy(false);
      setError(
        failure instanceof UnauthorizedError
          ? "The backend did not accept this token. Use DEVPILOT_API_TOKEN from the .env in the folder the running backend was started from; if you changed it, restart the backend."
          : failure instanceof ApiError
            ? failure.message
            : "The backend could not be reached.",
      );
      return;
    }
    setToken(SESSION, { remember }); // only a marker: the cookie is HttpOnly and the token is not kept. This card unmounts here
  }

  return (
    <div className="mx-auto mt-10 w-full max-w-md rounded-2xl border border-line bg-white p-6 shadow-soft sm:p-8">
      <span className="mb-4 grid size-11 place-items-center rounded-xl bg-violet-50 text-violet-600">
        <KeyRound className="size-5" aria-hidden />
      </span>
      <h1 className="text-lg font-semibold">Connect to your DevPilot backend</h1>
      <p className="mt-1.5 text-sm text-muted">
        Enter the API token the backend was started with (its <code className="rounded bg-slate-100 px-1 text-[12px]">DEVPILOT_API_TOKEN</code>). It is sent only to your local backend.
      </p>
      <form className="mt-5 space-y-3" onSubmit={submit}>
        <label htmlFor="api-token" className="block text-sm font-medium">
          API token
        </label>
        <input
          id="api-token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-full rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm outline-none transition focus:border-violet-400 focus:shadow-glow"
          aria-invalid={!!error}
          aria-describedby={error ? "api-token-error" : undefined}
        />
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} className="mt-0.5 size-4 shrink-0 accent-violet-600" />
          <span>
            Remember on this computer <span className="text-muted">(untick on a shared computer: the token is then kept in this tab only)</span>
          </span>
        </label>
        {error && (
          <div id="api-token-error">
            <Notice tone="error">{error}</Notice>
          </div>
        )}
        <button
          type="submit"
          disabled={busy}
          className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:brightness-110 disabled:opacity-60"
        >
          {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
          Connect
        </button>
      </form>
    </div>
  );
}
