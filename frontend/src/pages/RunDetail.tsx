import { ArrowLeft, FileJson, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApprovalPanel } from "../components/ApprovalPanel";
import { ActivityPanel } from "../components/ActivityPanel";
import { DeliveryCard } from "../components/DeliveryCard";
import { FileBrowser } from "../components/FileBrowser";
import { Notice } from "../components/Notice";
import { StatusBadge } from "../components/StatusBadge";
import { Timeline } from "../components/Timeline";
import { ApiError } from "../lib/api";
import { formatCost, formatDateTime, shortId } from "../lib/format";
import { useApi } from "../lib/apiContext";
import { DEFAULT_STACK } from "../lib/capabilities";
import { phaseMessage } from "../lib/phase";
import { assessReadiness } from "../lib/readiness";
import { isWorking, keys, LIST_POLL_MS, useAnalysis, useBudget, useCapabilities, usePlan, useProject, useProjectFiles, useReport, useRun, useRunEvents, useStatus } from "../lib/queries";
import { buildTimeline } from "../lib/timeline";
import { TERMINAL_STATES, type RunEvent } from "../lib/types";

function lastPayload(events: RunEvent[], type: string): Record<string, unknown> | undefined {
  return events.filter((e) => e.type === type).at(-1)?.payload;
}

export function RunDetail() {
  const { runId } = useParams();
  const location = useLocation();
  const routeState = (location.state ?? {}) as { startError?: string; projectName?: string };
  const run = useRun(runId);
  const project = useProject(run.data?.project_id);
  const events = useRunEvents(run.data);
  const recorded = new Set((events.data ?? []).map((event) => event.type));
  const plan = usePlan(run.data, recorded.has("plan.accepted"));
  const analysis = useAnalysis(run.data, recorded.has("analysis.accepted"));
  const report = useReport(run.data);
  const files = useProjectFiles(run.data?.project_id, isWorking(run.data) ? LIST_POLL_MS : false);
  const budget = useBudget();
  const capabilities = useCapabilities();
  const backendStatus = useStatus();
  const api = useApi();
  const queryClient = useQueryClient();
  const startLock = useRef(false);
  const [showReport, setShowReport] = useState(false);
  const [startApproval, setStartApproval] = useState(false);
  const [spendApproved, setSpendApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const timeline = useMemo(() => buildTimeline(events.data ?? []), [events.data]);
  const stackId = project.data?.stack ?? DEFAULT_STACK;
  const readiness = assessReadiness({
    capabilities: capabilities.data,
    budget: budget.data,
    status: backendStatus.data,
    stackId,
    failed: { capabilities: capabilities.error, budget: budget.error, status: backendStatus.error },
  });
  useEffect(() => {
    if (startApproval) setSpendApproved(false);
  }, [budget.data?.run_limit_usd, startApproval]);

  if (run.isLoading) {
    return (
      <p className="flex items-center gap-2 pt-10 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Loading run…
      </p>
    );
  }
  if (!run.data) {
    return (
      <div className="mx-auto max-w-3xl pt-10">
        <Notice tone="error" title="Run not found">
          {run.error instanceof Error ? run.error.message : "It may have been removed."}
        </Notice>
      </div>
    );
  }

  const r = run.data;
  const message = phaseMessage(r);
  const lastTest = lastPayload(events.data ?? [], "verification.finished");
  const lastAcceptance = lastPayload(events.data ?? [], "acceptance.finished");
  const projectName = project.data?.name ?? routeState.projectName ?? "Project";

  async function startWithApproval() {
    if (!runId || !budget.data || !spendApproved || busy || startLock.current) return;
    startLock.current = true;
    setBusy(true);
    setActionError(null);
    try {
      const [status, freshCapabilities, freshBudget] = await Promise.all([
        api.status(),
        api.getCapabilities(),
        api.getBudget(),
      ]);
      queryClient.setQueryData(keys.status, status);
      queryClient.setQueryData(keys.capabilities, freshCapabilities);
      queryClient.setQueryData(keys.budget, freshBudget);
      if (freshBudget.run_limit_usd !== budget.data.run_limit_usd) {
        setSpendApproved(false);
        setActionError("The spending limit changed while you were reviewing. Please approve the updated limit before starting.");
        return;
      }
      const fresh = assessReadiness({ capabilities: freshCapabilities, budget: freshBudget, status, stackId });
      if (!fresh.ready) {
        setActionError(fresh.summary);
        return;
      }
      await api.startRun(r.id, freshBudget.run_limit_usd);
      setStartApproval(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.run(r.id) }),
        queryClient.invalidateQueries({ queryKey: keys.events(r.id) }),
        queryClient.invalidateQueries({ queryKey: keys.runs }),
        queryClient.invalidateQueries({ queryKey: keys.budget }),
      ]);
    } catch (failure) {
      if (failure instanceof ApiError && [0, 401, 503].includes(failure.status)) {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: keys.status }),
          queryClient.invalidateQueries({ queryKey: keys.capabilities }),
          queryClient.invalidateQueries({ queryKey: keys.budget }),
        ]);
      }
      setActionError(failure instanceof Error ? failure.message : "The run could not be started.");
    } finally {
      startLock.current = false;
      setBusy(false);
    }
  }

  async function cancel() {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.cancelRun(r.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.run(r.id) }),
        queryClient.invalidateQueries({ queryKey: keys.events(r.id) }),
        queryClient.invalidateQueries({ queryKey: keys.runs }),
      ]);
    } catch (failure) {
      setActionError(failure instanceof Error ? failure.message : "The run could not be cancelled.");
    } finally {
      setBusy(false);
    }
  }

  const canCancel = !TERMINAL_STATES.has(r.state);
  const canOfferStart = canCancel && r.state !== "AWAITING_APPROVAL" && (r.state === "CREATED" || !!r.pause_reason);

  return (
    <div className="mx-auto max-w-6xl pt-6">
      <Link to="/runs" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden /> Runs
      </Link>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <h1 className="min-w-0 text-2xl font-bold tracking-tight [overflow-wrap:anywhere]">{projectName}</h1>
        <StatusBadge state={r.state} paused={!!r.pause_reason} />
      </div>
      <p className="mt-1 text-sm text-muted">
        Run <span className="font-mono">{shortId(r.id)}</span> · started {formatDateTime(r.started_at)}
        {r.ended_at ? ` · ended ${formatDateTime(r.ended_at)}` : ""} · {r.turns} GPT turns · {(r.tokens_in + r.tokens_out).toLocaleString()} tokens · {formatCost(r)}
      </p>

      <Notice tone={message.tone} title={message.title} className="mt-5">
        {message.text}
      </Notice>
      {routeState.startError && <Notice tone="error" title="The run was created but did not start">{routeState.startError}</Notice>}
      {actionError && !startApproval && <Notice tone="error" title="Run action failed">{actionError}</Notice>}
      {canCancel && (
        <div className="mt-4 flex flex-wrap gap-2">
          {canOfferStart && (
            <button type="button" onClick={() => { setSpendApproved(false); setActionError(null); setStartApproval(true); }} disabled={busy} className="rounded-xl bg-violet-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
              {r.state === "CREATED" ? "Start build" : "Resume build"}
            </button>
          )}
          <button type="button" onClick={() => void cancel()} disabled={busy} className="rounded-xl border border-line px-4 py-2 text-sm font-medium disabled:opacity-50">
            {busy ? "Working…" : "Cancel run"}
          </button>
        </div>
      )}
      {startApproval && (
        <div className="mt-4 rounded-2xl border border-violet-200 bg-white p-4 [overflow-wrap:anywhere] sm:p-5">
          <h2 className="font-semibold">Approve this run's spending limit</h2>
          <p className="mt-2 text-sm text-muted">{budget.data ? `Maximum spending: $${budget.data.run_limit_usd.toFixed(2)}` : "Checking the spending limit…"}</p>
          {!readiness.ready && <p role="alert" className="mt-3 text-sm text-rose-600">{actionError ?? readiness.summary}</p>}
          {readiness.ready && actionError && <p role="alert" className="mt-3 text-sm text-rose-600">{actionError}</p>}
          <label className="mt-3 flex items-start gap-2 text-sm">
            <input type="checkbox" checked={spendApproved} onChange={(event) => setSpendApproved(event.target.checked)} disabled={busy} className="mt-0.5 size-4 shrink-0 accent-violet-600" />
            <span>{budget.data ? `I approve spending up to $${budget.data.run_limit_usd.toFixed(2)} USD on this build.` : "I approve this build's spending limit."}</span>
          </label>
          <div className="mt-4 flex flex-col-reverse gap-2 sm:flex-row">
            <button type="button" onClick={() => { setStartApproval(false); setSpendApproved(false); setActionError(null); }} disabled={busy} className="min-h-10 rounded-lg border border-line px-3 py-1.5 text-sm">Cancel</button>
            <button type="button" onClick={() => void startWithApproval()} disabled={busy || !spendApproved || !readiness.ready} aria-busy={busy} className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-violet-600 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50">
              {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
              Approve and start
            </button>
          </div>
        </div>
      )}

      <div className="mt-5">
        <DeliveryCard run={r} report={report.data ?? null} projectName={projectName} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-6">
          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="text-sm font-semibold">Requirement</h2>
            <p className="mt-2 text-[15px] leading-relaxed whitespace-pre-wrap">{project.data?.requirement_text ?? "…"}</p>
          </section>
          <ApprovalPanel runId={r.id} />

          {(analysis.data || plan.data) && (
            <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
              <h2 className="text-sm font-semibold">Plan</h2>
              {analysis.data && <p className="mt-2 text-sm text-slate-700">{analysis.data.summary}</p>}
              {plan.data && (
                <>
                  <p className="mt-3 text-xs text-muted">
                    Stack <span className="font-medium text-ink">{plan.data.stack}</span> · {plan.data.tasks.length} task{plan.data.tasks.length === 1 ? "" : "s"}
                    {analysis.data ? ` · ${analysis.data.requirements.length} requirements` : ""}
                  </p>
                  <ol className="mt-3 space-y-2">
                    {plan.data.tasks.map((task) => (
                      <li key={task.order} className="flex gap-3 rounded-xl bg-slate-50 p-3">
                        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-white text-xs font-semibold text-violet-700 ring-1 ring-line">{task.order}</span>
                        <div className="min-w-0">
                          <p className="text-sm font-medium">{task.title}</p>
                          <p className="text-xs text-muted">
                            {task.capability.replaceAll("_", " ")} · {task.status.toLowerCase()} · covers {task.requirement_refs.join(", ")}
                          </p>
                        </div>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </section>
          )}

          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="mb-4 text-sm font-semibold">Activity</h2>
            {events.isError ? <p className="text-sm text-rose-600">The run's events could not be loaded.</p> : <Timeline items={timeline} />}
          </section>
        </div>

        <aside className="space-y-6">
          <ActivityPanel run={r} events={events.data ?? []} />

          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="text-sm font-semibold">Verification</h2>
            <dl className="mt-3 space-y-2 text-[13px]">
              <div className="flex justify-between gap-3">
                <dt className="text-muted">Engine's own tests</dt>
                <dd className="text-right font-medium">{lastTest ? `${String(lastTest.verdict)} · ${String(lastTest.passed)} of ${String(lastTest.ran)}` : "Not run"}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted">Independent acceptance</dt>
                <dd className="text-right font-medium">
                  {lastAcceptance
                    ? `${String(lastAcceptance.verdict)} · ${String(lastAcceptance.passed)} of ${String(lastAcceptance.total)}`
                    : project.data?.has_acceptance_suite
                      ? "Not run yet"
                      : "Not configured"}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted">Sandbox</dt>
                <dd className="text-right font-medium">{report.data ? report.data.provenance.sandbox : "—"}</dd>
              </div>
            </dl>
          </section>

          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="mb-3 text-sm font-semibold">Files</h2>
            {files.isError ? <p className="text-[13px] text-rose-600">The files could not be listed.</p> : <FileBrowser projectId={r.project_id} paths={files.data?.files ?? []} />}
          </section>

          <section className="rounded-2xl border border-line bg-white p-5 shadow-soft">
            <h2 className="text-sm font-semibold">Artifacts</h2>
            {report.data ? (
              <>
                <button type="button" onClick={() => setShowReport((v) => !v)} aria-expanded={showReport} className="mt-3 inline-flex items-center gap-2 rounded-xl border border-line px-3 py-2 text-[13px] font-medium hover:bg-slate-50">
                  <FileJson className="size-4 text-violet-600" aria-hidden />
                  {showReport ? "Hide" : "Show"} the final report
                </button>
                {showReport && <pre className="mt-3 max-h-96 overflow-auto rounded-xl bg-slate-50 p-3 font-mono text-[11px] leading-relaxed">{JSON.stringify(report.data, null, 2)}</pre>}
              </>
            ) : (
              <p className="mt-2 text-[13px] text-muted">The final report is written when the run ends.</p>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
