import { CheckCircle2, ExternalLink, FolderGit2 as Github, HardDrive, Loader2, ShieldAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useApi } from "../lib/apiContext";
import type { ResourceCheck, ResourceItem } from "../lib/types";

// A small popup for the one thing only the person can do: grant access to a repository or document their request names.
// The consent page opens in its own window; the backend keeps every token. Once access is granted the resources are checked
// again and the build continues on its own, so nothing has to be retyped.

const POLL_MS = 2_000;
const GIVE_UP_MS = 10 * 60_000; // the backend's consent state expires after ten minutes

/** Opens a window synchronously (so the click is not blocked as a popup) and points it at the URL once it is known. */
async function openConsent(load: () => Promise<{ url: string }>): Promise<Window | null> {
  const opened = window.open("about:blank", "devpilot-consent", "popup,width=640,height=760");
  try {
    const { url } = await load();
    if (opened) opened.location.href = url;
    else window.location.assign(url); // popups blocked: continue in this tab
    return opened;
  } catch (failure) {
    opened?.close();
    throw failure;
  }
}

function ResourceRow({ item }: { item: ResourceItem }) {
  const Icon = item.kind === "github" ? Github : HardDrive;
  const ok = item.status === "accessible";
  return (
    <li className="flex gap-3 rounded-xl border border-line px-3 py-2.5 text-sm">
      <Icon className="mt-0.5 size-4 shrink-0 text-slate-600" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-medium [overflow-wrap:anywhere]">{item.full_name ?? item.label}</p>
        <p className="text-[13px] text-muted">{item.message}</p>
      </div>
      {ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-label="Readable" /> : <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600" aria-label="Needs permission" />}
    </li>
  );
}

export function PermissionDialog({
  projectId,
  initial,
  onReady,
  onSkip,
  onCancel,
}: {
  projectId: string;
  initial: ResourceCheck;
  onReady: (checked: ResourceCheck) => void;
  onSkip: () => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const [checked, setChecked] = useState(initial);
  const [waiting, setWaiting] = useState<"github" | "google" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const deadline = useRef(0);
  const needs = checked.resources.filter((item) => item.action);
  const wantsGitHub = needs.some((item) => item.action === "connect_github");
  const wantsGoogle = needs.some((item) => item.action === "connect_google");

  useEffect(() => () => {
    if (timer.current !== null) window.clearInterval(timer.current);
  }, []);

  function stopWaiting() {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
    setWaiting(null);
  }

  function waitFor(kind: "github" | "google") {
    setWaiting(kind);
    deadline.current = Date.now() + GIVE_UP_MS;
    timer.current = window.setInterval(() => {
      void (async () => {
        if (Date.now() > deadline.current) {
          stopWaiting();
          setError("Permission was not granted within ten minutes. Try again.");
          return;
        }
        try {
          if (kind === "github" && !(await api.githubStatus()).connected) return;
          const fresh = await api.checkResources(projectId);
          const still = fresh.resources.some((item) => item.action === (kind === "github" ? "connect_github" : "connect_google"));
          if (kind === "google" && still) return;
          stopWaiting();
          setChecked(fresh);
          if (fresh.needs_permission.length === 0) onReady(fresh); // the build continues by itself
        } catch {
          // keep waiting: the backend may be busy for a moment
        }
      })();
    }, POLL_MS);
  }

  async function connect(kind: "github" | "google") {
    setError(null);
    try {
      await openConsent(() => (kind === "github" ? api.connectGitHub() : api.authorizeGoogle(projectId)));
      waitFor(kind);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The permission page could not be opened.");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/30 p-2 sm:items-center sm:p-4" role="presentation">
      <section role="dialog" aria-modal="true" aria-labelledby="permission-title" className="flex max-h-[calc(100dvh-1rem)] w-full max-w-md flex-col rounded-2xl border border-line bg-white shadow-xl">
        <h2 id="permission-title" className="border-b border-line px-5 pt-4 pb-3 text-lg font-semibold">Permission needed</h2>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <p className="text-sm text-slate-700">Your request names resources DevPilot cannot read yet. Grant access once and the build continues on its own.</p>
          <ul className="mt-3 space-y-2">
            {checked.resources.map((item) => (
              <ResourceRow key={item.key} item={item} />
            ))}
          </ul>
          {waiting && (
            <p className="mt-3 flex items-center gap-2 text-sm text-indigo-700" role="status">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Waiting for you to finish in the {waiting === "github" ? "GitHub" : "Google"} window…
            </p>
          )}
          {error && <p role="alert" className="mt-3 text-sm text-rose-600">{error}</p>}
        </div>
        <div className="flex flex-col gap-2 border-t border-line px-5 py-3 sm:flex-row sm:flex-wrap sm:justify-end">
          <button type="button" onClick={onCancel} className="min-h-10 rounded-xl border border-line px-4 py-2 text-sm font-medium hover:bg-slate-50">Cancel</button>
          <button type="button" onClick={onSkip} disabled={waiting !== null} className="min-h-10 rounded-xl border border-line px-4 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-50">Build without it</button>
          {wantsGitHub && (
            <button type="button" disabled={waiting !== null} onClick={() => void connect("github")} className="inline-flex min-h-10 items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
              <Github className="size-4" aria-hidden /> Connect GitHub <ExternalLink className="size-3.5" aria-hidden />
            </button>
          )}
          {wantsGoogle && (
            <button type="button" disabled={waiting !== null} onClick={() => void connect("google")} className="inline-flex min-h-10 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
              <HardDrive className="size-4" aria-hidden /> Allow Google Drive access <ExternalLink className="size-3.5" aria-hidden />
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
