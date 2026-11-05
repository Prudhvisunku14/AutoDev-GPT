import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Loader2, Sparkles } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ApiError } from "../lib/api";
import { useApi } from "../lib/apiContext";
import { projectNameFrom } from "../lib/capabilities";
import { validatePrompt, verifiableStacks } from "../lib/prompt";
import { assessReadiness } from "../lib/readiness";
import { keys, useBudget, useCapabilities, useStatus } from "../lib/queries";
import { cn } from "../lib/cn";
import type { Project, ResourceCheck, Run } from "../lib/types";
import { BuildStatus } from "./BuildStatus";
import { PermissionDialog } from "./PermissionDialog";

const MAX_HEIGHT = 320;

/**
 * Collects a requirement, then asks for explicit approval of the spending limit the backend enforces before creating
 * and starting a run. No stack is chosen here: GPT analyses the request and picks one from the backend's catalog, and the
 * backend's code refuses to code or deliver a stack it cannot verify. The dialog lists the stacks this backend can verify.
 * Whether a build may start is the backend's answer (assessReadiness), checked again on fresh data at the moment of
 * approval and once more by the backend itself when the run starts.
 */
export function Composer({ initialPrompt = "", onCreated }: { initialPrompt?: string; onCreated?: (project: Project, run: Run, error?: string) => void }) {
  const [prompt, setPrompt] = useState(initialPrompt);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [spendApproved, setSpendApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  // A project whose request names a repository or document DevPilot cannot read yet: the run waits for the person's permission.
  const [gate, setGate] = useState<{ project: Project; run: Run; limit: number; checked: ResourceCheck } | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const submissionLock = useRef(false);
  const ids = { hint: useId(), error: useId() };
  const api = useApi();
  const client = useQueryClient();
  const capabilities = useCapabilities();
  const budget = useBudget();
  const backendStatus = useStatus();
  const buildable = verifiableStacks(capabilities.data?.stacks);
  const readiness = assessReadiness({
    capabilities: capabilities.data,
    budget: budget.data,
    status: backendStatus.data,
    stackId: null, // GPT chooses
    failed: { capabilities: capabilities.error, budget: budget.error, status: backendStatus.error },
  });
  const unsupported: string | null = null;
  const loading = capabilities.isLoading || budget.isLoading || backendStatus.isLoading;

  useEffect(() => {
    if (confirming) setSpendApproved(false); // a changed limit needs a new approval
  }, [budget.data?.run_limit_usd, confirming]);

  useEffect(() => {
    if (confirming) dialog.current?.focus(); // keyboard and screen-reader users start inside the dialog, where Escape works
  }, [confirming]);

  useLayoutEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`;
  }, [prompt]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (busy) return;
    const problem = validatePrompt(prompt) ?? unsupported;
    if (problem) {
      setError(problem);
      textarea.current?.focus();
      return;
    }
    if (readiness.kind === "checking") return;
    setError(null);
    setSpendApproved(false);
    setConfirming(true);
  }

  function cancelReview() {
    setConfirming(false);
    setSpendApproved(false);
    setError(null);
  }

  async function approveAndStart() {
    if (!budget.data || !spendApproved || busy || submissionLock.current) return;
    submissionLock.current = true; // a second click while the first is in flight does nothing
    setBusy(true);
    setError(null);
    try {
      const [status, freshCapabilities, freshBudget] = await Promise.all([api.status(), api.getCapabilities(), api.getBudget()]);
      client.setQueryData(keys.status, status);
      client.setQueryData(keys.capabilities, freshCapabilities);
      client.setQueryData(keys.budget, freshBudget);
      if (freshBudget.run_limit_usd !== budget.data.run_limit_usd) {
        setSpendApproved(false);
        setError("The spending limit changed. Please approve the new limit.");
        return;
      }
      const fresh = assessReadiness({ capabilities: freshCapabilities, budget: freshBudget, status, stackId: null }); // the backend may have changed
      if (!fresh.ready) {
        setError(fresh.summary);
        return;
      }
      // The request goes as written: GPT chooses the stack, and the backend refuses any it cannot verify.
      const project = await api.createProject({ name: projectNameFrom(prompt), requirement_text: prompt.trim() });
      const run = await api.createRun(project.id);
      // Repositories and documents the request names are read now, so GPT starts from what they contain. One that needs the
      // person's permission pauses here, before anything is spent.
      const checked = await api.checkResources(project.id).catch(() => null);
      if (checked && checked.needs_permission.length > 0) {
        setConfirming(false);
        setGate({ project, run, limit: freshBudget.run_limit_usd, checked });
        return;
      }
      await begin(project, run, freshBudget.run_limit_usd);
    } catch (failure) {
      if (failure instanceof ApiError && [0, 401, 503].includes(failure.status)) {
        await Promise.all([
          client.invalidateQueries({ queryKey: keys.status }),
          client.invalidateQueries({ queryKey: keys.capabilities }),
          client.invalidateQueries({ queryKey: keys.budget }),
        ]);
      }
      setError(failure instanceof Error ? failure.message : "The build could not be prepared.");
    } finally {
      submissionLock.current = false;
      setBusy(false);
    }
  }

  async function begin(project: Project, run: Run, limit: number) {
    try {
      await api.startRun(run.id, limit);
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "The run could not be started.";
      await Promise.all([client.invalidateQueries({ queryKey: keys.projects }), client.invalidateQueries({ queryKey: keys.runs })]);
      setConfirming(false);
      onCreated?.(project, run, message);
      return;
    }
    await Promise.all([client.invalidateQueries({ queryKey: keys.projects }), client.invalidateQueries({ queryKey: keys.runs })]);
    setPrompt("");
    setConfirming(false);
    onCreated?.(project, run);
  }

  async function continueAfterPermission() {
    if (!gate) return;
    const { project, run, limit } = gate;
    setGate(null);
    setBusy(true);
    try {
      await begin(project, run, limit);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
  }

  const describedBy = [ids.hint, error ? ids.error : null].filter(Boolean).join(" ");
  const shownProblem = error ?? (prompt.trim() ? unsupported : null);

  return (
    <>
      <form onSubmit={submit} noValidate aria-label="Describe the application to build" className="w-full">
        <div
          className={cn(
            "rounded-3xl border bg-white/95 p-4 shadow-soft transition sm:p-5",
            error ? "border-rose-300" : "border-violet-200 focus-within:border-violet-400 focus-within:shadow-glow",
          )}
        >
          <label htmlFor="prompt" className="flex items-center gap-2 text-sm font-semibold text-violet-700">
            <Sparkles className="size-4" aria-hidden />
            Describe the application you want to build
          </label>
          <textarea
            id="prompt"
            ref={textarea}
            value={prompt}
            onChange={(e) => {
              setPrompt(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={onKeyDown}
            rows={3}
            placeholder="Describe the application you want to build..."
            aria-invalid={!!shownProblem}
            aria-describedby={describedBy}
            disabled={busy}
            className="mt-3 block w-full resize-none bg-transparent text-[15px] leading-relaxed outline-none placeholder:text-slate-400 disabled:opacity-60"
          />
          <div className="mt-4 flex items-center justify-end">
            <button
              type="submit"
              disabled={busy || loading}
              aria-label="Build Application"
              title="Review the spending limit before starting (Ctrl+Enter)"
              className="inline-flex h-11 items-center gap-2 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 px-5 text-sm font-semibold text-white shadow-[0_8px_20px_-8px_rgb(109_40_217/0.7)] transition hover:brightness-110 disabled:opacity-60"
            >
              {busy ? <Loader2 className="size-5 animate-spin" aria-hidden /> : <ArrowUp className="size-5" aria-hidden />}
              Build Application
            </button>
          </div>
        </div>

        <div className="mt-3 space-y-2 px-1" id={ids.hint}>
          {shownProblem && !confirming && <p id={ids.error} role="alert" className="text-sm text-rose-600">{shownProblem}</p>}
          <BuildStatus readiness={readiness} />
        </div>
      </form>

      {gate && (
        <PermissionDialog
          projectId={gate.project.id}
          initial={gate.checked}
          onReady={() => void continueAfterPermission()}
          onSkip={() => void continueAfterPermission()}
          onCancel={() => {
            const { project, run } = gate;
            setGate(null);
            onCreated?.(project, run, "The build was not started: permission for a named resource was not granted.");
          }}
        />
      )}

      {confirming && budget.data && (
        // The overlay never scrolls the page behind it. The dialog is as tall as the viewport allows: its body scrolls,
        // and the approval checkbox and both buttons stay in the footer, reachable at any height.
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/30 p-2 sm:items-center sm:p-4" role="presentation">
          <section
            ref={dialog}
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-labelledby="spend-title"
            onKeyDown={(event) => {
              if (event.key === "Escape" && !busy) cancelReview();
            }}
            className="flex max-h-[calc(100dvh-1rem)] w-full max-w-md min-w-0 flex-col rounded-2xl border border-line bg-white shadow-xl outline-none sm:max-h-[calc(100dvh-2rem)]"
          >
            <h2 id="spend-title" className="shrink-0 border-b border-line px-4 pt-4 pb-3 text-lg font-semibold sm:px-6 sm:pt-5">Review build spending</h2>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-4 [overflow-wrap:anywhere] sm:px-6" data-testid="spend-body">
              <dl className="space-y-2 rounded-xl bg-slate-50 p-3 text-sm sm:p-4">
                <div className="flex justify-between gap-3"><dt className="shrink-0 text-muted">Application</dt><dd className="min-w-0 text-right font-medium">{projectNameFrom(prompt)}</dd></div>
                <div className="flex justify-between gap-3"><dt className="shrink-0 text-muted">Builds as</dt><dd className="min-w-0 text-right font-medium">{buildable.length ? `Chosen by GPT from: ${buildable.map((stack) => stack.name).join(", ")}` : "Checking the backend…"}</dd></div>
                <div className="flex justify-between gap-3"><dt className="text-muted">Maximum spending</dt><dd className="font-semibold">${budget.data.run_limit_usd.toFixed(2)}</dd></div>
              </dl>
              {(error || !readiness.ready) && (
                <p role="alert" className="mt-3 text-sm text-rose-600">{error ?? readiness.summary}</p>
              )}
            </div>
            <div className="shrink-0 border-t border-line px-4 py-3 sm:px-6 sm:py-4">
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" checked={spendApproved} onChange={(event) => setSpendApproved(event.target.checked)} disabled={busy} className="mt-0.5 size-4 shrink-0 accent-violet-600" />
                <span>I approve spending up to <strong>${budget.data.run_limit_usd.toFixed(2)} USD</strong> on this build.</span>
              </label>
              <div className="mt-3 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <button type="button" disabled={busy} onClick={cancelReview} className="min-h-10 rounded-xl border border-line px-4 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-60">Cancel</button>
                <button
                  type="button"
                  disabled={busy || !spendApproved || !readiness.ready}
                  aria-busy={busy}
                  onClick={() => void approveAndStart()}
                  className="inline-flex min-h-10 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
                  Approve and start build
                </button>
              </div>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
