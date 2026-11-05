import { Bell, LogOut, Menu, Server } from "lucide-react";
import { Link } from "react-router";
import { useApprovals, useStatus } from "../../lib/queries";
import { useApi } from "../../lib/apiContext";
import { clearToken } from "../../lib/token";
import { BudgetIndicator } from "../BudgetIndicator";
import { Popover } from "../Popover";

function Notifications() {
  const approvals = useApprovals();
  const pending = approvals.data ?? [];
  if (!pending.length) return null; // no bell without something to say
  return (
    <Popover
      label={`${pending.length} pending approval${pending.length === 1 ? "" : "s"}`}
      buttonClassName="relative grid size-9 place-items-center rounded-full text-slate-600 transition hover:bg-violet-50"
      button={
        <>
          <Bell className="size-5" aria-hidden />
          <span className="absolute top-1.5 right-1.5 size-2 rounded-full bg-violet-600 ring-2 ring-white" aria-hidden />
        </>
      }
    >
      <p className="font-semibold">Waiting for a decision</p>
      <ul className="mt-2 space-y-2">
        {pending.map((approval) => (
          <li key={approval.id} className="rounded-xl bg-slate-50 p-3">
            <p className="font-medium">{approval.kind === "HUMAN_INPUT" ? "GPT asked a question" : `Approve ${approval.tool}?`}</p>
            <Link to={`/runs/${approval.run_id}`} className="text-xs text-violet-700 hover:underline">
              Open the run
            </Link>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-muted">Open a run to review and decide. A decision can continue its approved build.</p>
    </Popover>
  );
}

function AccountMenu() {
  const status = useStatus();
  const api = useApi();
  async function signOut() {
    await api.signOut().catch(() => undefined); // ends the session cookie on the backend; forgetting it here happens either way
    clearToken();
  }
  return (
    <Popover
      label="Account and backend"
      buttonClassName="size-9 rounded-full bg-gradient-to-br from-violet-400 via-indigo-400 to-blue-400 ring-2 ring-white shadow-sm"
      button={<span className="sr-only">Account</span>}
      panelClassName="w-72"
    >
      <p className="flex items-center gap-2 font-semibold">
        <Server className="size-4 text-violet-600" aria-hidden />
        Local backend
      </p>
      {status.data ? (
        <dl className="mt-2 space-y-1 text-[13px]">
          <div className="flex justify-between">
            <dt className="text-muted">Status</dt>
            <dd className="font-medium">{status.data.status}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted">Version</dt>
            <dd className="font-medium">{status.data.version}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted">Database</dt>
            <dd className="font-medium">{status.data.database}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted">Projects · runs</dt>
            <dd className="font-medium">
              {status.data.projects} · {status.data.runs}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="mt-2 text-[13px] text-muted">{status.isError ? "The backend status could not be read." : "Loading…"}</p>
      )}
      <button type="button" onClick={() => void signOut()} className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-line px-3 py-2 text-[13px] font-medium hover:bg-slate-50">
        <LogOut className="size-4" aria-hidden />
        Forget the API token
      </button>
    </Popover>
  );
}

export function Header({ onMenu, connected }: { onMenu: () => void; connected: boolean }) {
  return (
    <header className="flex items-center gap-3 px-4 pt-4 sm:px-8 sm:pt-5">
      <button type="button" onClick={onMenu} aria-label="Open navigation" className="grid size-10 place-items-center rounded-xl text-slate-700 hover:bg-violet-50 md:hidden">
        <Menu className="size-5" aria-hidden />
      </button>
      <div className="ml-auto flex items-center gap-2 sm:gap-3">
        {connected && (
          <>
            <BudgetIndicator />
            <Notifications />
            <AccountMenu />
          </>
        )}
      </div>
    </header>
  );
}
