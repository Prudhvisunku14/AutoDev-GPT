import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router";
import { cn } from "../../lib/cn";
import { useToken } from "../../lib/token";
import { useMediaQuery } from "../../lib/useMediaQuery";
import { ConnectCard } from "../ConnectCard";
import { Header } from "./Header";
import { SidebarContent } from "./Sidebar";

const COLLAPSED_KEY = "devpilot-sidebar-collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** Sidebar, header and the page. Without an API token, every page shows the connect card instead. */
export function AppLayout() {
  const token = useToken();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawer, setDrawer] = useState(false);
  const wide = useMediaQuery("(min-width: 1024px)");
  const narrow = collapsed || !wide;

  useEffect(() => setDrawer(false), [location.pathname]);

  function toggle() {
    setCollapsed((value) => {
      try {
        window.localStorage.setItem(COLLAPSED_KEY, value ? "0" : "1");
      } catch {
        // the choice is only remembered when storage works
      }
      return !value;
    });
  }

  return (
    <div className="app-backdrop flex min-h-dvh">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:shadow">
        Skip to content
      </a>

      {/* Tablet: icons only. Desktop: full width unless collapsed. Phone: the drawer below. */}
      <aside className={cn("sticky top-0 hidden h-dvh shrink-0 border-r border-line bg-white/70 backdrop-blur transition-[width] md:block", narrow ? "w-[76px]" : "w-60")}>
        <SidebarContent narrow={narrow} onToggle={wide ? toggle : undefined} />
      </aside>

      {/* Phone: a drawer. */}
      {drawer && (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button type="button" aria-label="Close navigation" className="absolute inset-0 bg-slate-900/20 backdrop-blur-sm" onClick={() => setDrawer(false)} />
          <div className="absolute inset-y-0 left-0 w-72 border-r border-line bg-white shadow-xl">
            <button type="button" onClick={() => setDrawer(false)} aria-label="Close navigation" className="absolute top-5 right-4 grid size-9 place-items-center rounded-lg hover:bg-violet-50">
              <X className="size-5" aria-hidden />
            </button>
            <SidebarContent narrow={false} onNavigate={() => setDrawer(false)} />
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <Header onMenu={() => setDrawer(true)} connected={!!token} />
        <main id="main" className="flex-1 px-4 pb-12 sm:px-8">
          {token ? <Outlet /> : <ConnectCard />}
        </main>
      </div>
    </div>
  );
}
