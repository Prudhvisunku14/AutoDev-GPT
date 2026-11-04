import { useState } from "react";
import { ApiError } from "../lib/api";
import { useApi } from "../lib/apiContext";
import { keys, useRunApprovals } from "../lib/queries";
import { useQueryClient } from "@tanstack/react-query";
import type { Approval } from "../lib/types";
import { Notice } from "./Notice";

function detailsText(approval: Approval): string | null {
  const details = approval.details;
  if (!details || typeof details !== "object" || Array.isArray(details)) return null;
  const record = details as Record<string, unknown>;
  const summary = ["operation", "reason", "names", "locations"].flatMap((key) => {
    const value = record[key];
    if (typeof value === "string" && value) return [value];
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) return [`${key}: ${value.join(", ")}`];
    return [];
  });
  return summary.length ? summary.join(" · ") : null;
}

export function ApprovalPanel({ runId }: { runId: string }) {
  const approvals = useRunApprovals(runId);
  const api = useApi();
  const client = useQueryClient();
  const [responses, setResponses] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(id: string, decision: "approve" | "deny") {
    if (busy) return;
    setBusy(id);
    setError(null);
    try {
      await api.decideApproval(id, decision, responses[id]);
      await Promise.all([
        client.invalidateQueries({ queryKey: [...keys.approvals, runId] }),
        client.invalidateQueries({ queryKey: keys.approvals }),
        client.invalidateQueries({ queryKey: keys.run(runId) }),
        client.invalidateQueries({ queryKey: keys.events(runId) }),
      ]);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : failure instanceof Error ? failure.message : "The approval decision failed.");
    } finally {
      setBusy(null);
    }
  }

  if (approvals.isLoading) return <p className="text-sm text-muted">Checking for pending decisions…</p>;
  if (approvals.isError) {
    return <Notice tone="error" title="Pending decisions could not be loaded">{approvals.error instanceof Error ? approvals.error.message : "Unknown backend error."}</Notice>;
  }
  if (!approvals.data?.length) return null;

  return (
    <section aria-labelledby="approval-title" className="rounded-2xl border border-amber-200 bg-amber-50/60 p-5">
      <h2 id="approval-title" className="text-sm font-semibold">Your decision is needed</h2>
      <ul className="mt-3 space-y-3">
        {approvals.data.map((approval) => (
          <li key={approval.id} className="rounded-xl border border-amber-100 bg-white p-4">
            <p className="text-sm font-medium">{approval.kind === "HUMAN_INPUT" ? "The build needs your input" : `Review ${approval.tool}`}</p>
            <p className="mt-1 text-xs text-muted">Risk: {approval.risk} · {approval.kind}</p>
            {detailsText(approval) && <p className="mt-2 text-sm text-slate-700">{detailsText(approval)}</p>}
            {approval.kind === "HUMAN_INPUT" && (
              <label className="mt-3 block text-xs font-medium">
                Your response
                <textarea
                  value={responses[approval.id] ?? ""}
                  onChange={(event) => setResponses((current) => ({ ...current, [approval.id]: event.target.value }))}
                  rows={2}
                  maxLength={5000}
                  className="mt-1 w-full rounded-lg border border-line p-2 text-sm font-normal"
                  disabled={!!busy}
                />
              </label>
            )}
            <div className="mt-3 flex gap-2">
              <button type="button" disabled={!!busy} onClick={() => void decide(approval.id, "deny")} className="rounded-lg border border-line px-3 py-1.5 text-xs font-medium disabled:opacity-60">
                Reject
              </button>
              <button
                type="button"
                disabled={!!busy || (approval.kind === "HUMAN_INPUT" && !(responses[approval.id] ?? "").trim())}
                onClick={() => void decide(approval.id, "approve")}
                className="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
              >
                {busy === approval.id ? "Sending…" : approval.kind === "HUMAN_INPUT" ? "Submit response" : "Approve"}
              </button>
            </div>
          </li>
        ))}
      </ul>
      {error && <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p>}
    </section>
  );
}
