import { Wallet } from "lucide-react";
import { useBudget } from "../lib/queries";
import { Popover } from "./Popover";

/** The enforced limit and actual ledger returned by this backend; no future cost is presented as an estimate. */
export function BudgetIndicator() {
  const budget = useBudget();
  const data = budget.data;
  const label = data ? `Limit $${data.run_limit_usd.toFixed(2)}` : "Budget unavailable";

  return (
    <Popover
      label="Run budget"
      buttonClassName="inline-flex h-9 items-center gap-2 rounded-full border border-line bg-white/80 px-3.5 text-[13px] font-medium text-slate-700 shadow-sm transition hover:border-violet-200"
      button={
        <>
          <Wallet className="size-4 text-violet-600" aria-hidden />
          <span>{budget.isLoading ? "Loading budget…" : label}</span>
        </>
      }
    >
      <p className="font-semibold">Run budget</p>
      {budget.isError ? (
        <p className="mt-1.5 text-rose-600">{budget.error instanceof Error ? budget.error.message : "The backend's budget could not be read."}</p>
      ) : data ? (
        <>
          <p className="mt-1.5 text-muted">{data.estimate}</p>
          <dl className="mt-3 space-y-1.5 rounded-xl bg-slate-50 p-3">
            <div className="flex justify-between gap-3"><dt className="text-muted">Maximum per run</dt><dd className="font-medium">${data.run_limit_usd.toFixed(2)}</dd></div>
            <div className="flex justify-between gap-3"><dt className="text-muted">Recorded spend</dt><dd className="font-medium">${data.recorded.total_usd.toFixed(4)}</dd></div>
            <div className="flex justify-between gap-3"><dt className="text-muted">Active runs</dt><dd className="font-medium">{data.recorded.active_runs}</dd></div>
            <div className="flex justify-between gap-3"><dt className="text-muted">Unknown-cost runs</dt><dd className="font-medium">{data.recorded.unknown_cost_runs}</dd></div>
            {data.session && <div className="flex justify-between gap-3"><dt className="text-muted">Session remaining</dt><dd className="font-medium">${data.session.remaining_usd.toFixed(2)}</dd></div>}
          </dl>
          {!data.cost_known && <p className="mt-2 text-xs font-medium text-rose-600">Prices are not configured; paid builds are blocked.</p>}
        </>
      ) : (
        <p className="mt-1.5 text-muted">Loading the backend's enforced limit and recorded spending…</p>
      )}
    </Popover>
  );
}
