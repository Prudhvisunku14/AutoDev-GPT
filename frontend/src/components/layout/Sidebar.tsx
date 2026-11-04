import { FolderOpen, LayoutGrid, PanelLeftClose, PanelLeftOpen, PlayCircle, Plus } from "lucide-react";
import { NavLink, useNavigate } from "react-router";
import { cn } from "../../lib/cn";
import { Logo } from "../Logo";

export const NAV = [
  { to: "/projects", label: "Projects", icon: FolderOpen },
  { to: "/templates", label: "Templates", icon: LayoutGrid },
  { to: "/runs", label: "Runs", icon: PlayCircle },
] as const;

/** The navigation itself, shared by the fixed sidebar and the mobile drawer. `narrow` shows icons only. */
export function SidebarContent({ narrow, onNavigate, onToggle }: { narrow: boolean; onNavigate?: () => void; onToggle?: () => void }) {
  const navigate = useNavigate();
  return (
    <div className="flex h-full flex-col">
      <div className={cn("flex items-center px-5 pt-6 pb-8", narrow ? "justify-center px-0" : "justify-between")}>
        <NavLink to="/" onClick={onNavigate} aria-label="DevPilot AI home" className="rounded-xl">
          <Logo compact={narrow} />
        </NavLink>
      </div>
      <nav aria-label="Main" className="flex-1 space-y-1 px-3">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            title={narrow ? label : undefined}
            className={({ isActive }) =>
              cn(
                "group flex items-center gap-3 rounded-xl px-3 py-2.5 text-[15px] font-medium transition",
                narrow && "justify-center",
                isActive ? "bg-gradient-to-r from-violet-100/80 to-indigo-50 text-indigo-700" : "text-slate-700 hover:bg-violet-50/60 hover:text-slate-900",
              )
            }
          >
            <Icon className="size-5 shrink-0" aria-hidden />
            <span className={cn(narrow && "sr-only")}>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className={cn("space-y-3 p-4", narrow && "px-3")}>
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            aria-label={narrow ? "Expand the sidebar" : "Collapse the sidebar"}
            className={cn("hidden items-center gap-2 rounded-xl px-3 py-2 text-sm text-muted transition hover:bg-violet-50/60 lg:flex", narrow && "w-full justify-center")}
          >
            {narrow ? <PanelLeftOpen className="size-4" aria-hidden /> : <PanelLeftClose className="size-4" aria-hidden />}
            <span className={cn(narrow && "sr-only")}>Collapse</span>
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            onNavigate?.();
            navigate("/", { state: { focusComposer: true } });
          }}
          title={narrow ? "New Project" : undefined}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-3 text-sm font-semibold text-white shadow-[0_10px_24px_-10px_rgb(109_40_217/0.7)] transition hover:brightness-110"
        >
          <Plus className="size-4 shrink-0" aria-hidden />
          <span className={cn(narrow && "sr-only")}>New Project</span>
        </button>
      </div>
    </div>
  );
}
