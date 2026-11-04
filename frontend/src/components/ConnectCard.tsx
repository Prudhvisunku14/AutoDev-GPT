import { KeyRound, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError, UnauthorizedError } from "../lib/api";
import { useApi } from "../lib/apiContext";
import { setToken } from "../lib/token";
import { Notice } from "./Notice";

/**
 * Asks for the backend's API token and checks it against /status before keeping it, so a wrong token is never stored
 * and this card stays on screen to say why. The token stays in this tab only.
 */
export function ConnectCard() {
  const api = useApi();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const token = value.trim();
    if (!token) {
      setError("Paste the backend's API token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.checkToken(token);
    } catch (failure) {
      setBusy(false);
      setError(failure instanceof UnauthorizedError ? "The backend did not accept this token." : failure instanceof ApiError ? failure.message : "The backend could not be reached.");
      return;
    }
    setToken(token); // this card unmounts here: the layout now shows the page
  }

  return (
    <div className="mx-auto mt-10 w-full max-w-md rounded-2xl border border-line bg-white p-6 shadow-soft sm:p-8">
      <span className="mb-4 grid size-11 place-items-center rounded-xl bg-violet-50 text-violet-600">
        <KeyRound className="size-5" aria-hidden />
      </span>
      <h1 className="text-lg font-semibold">Connect to your DevPilot backend</h1>
      <p className="mt-1.5 text-sm text-muted">
        Enter the API token the backend was started with (its <code className="rounded bg-slate-100 px-1 text-[12px]">DEVPILOT_API_TOKEN</code>). It is kept only in this browser tab and sent only to your local backend.
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
