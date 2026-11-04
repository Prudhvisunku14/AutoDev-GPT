import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router";
import { ActivityPanel } from "../components/ActivityPanel";
import { Composer } from "../components/Composer";
import { useAllRuns, useRunEvents } from "../lib/queries";

export function Home() {
  const navigate = useNavigate();
  const location = useLocation();
  const state = (location.state ?? {}) as { prompt?: string; focusComposer?: boolean };
  const all = useAllRuns();
  const latest = all.entries[0] ?? null;
  const events = useRunEvents(latest?.run);

  useEffect(() => {
    if (state.focusComposer || state.prompt) document.getElementById("prompt")?.focus();
  }, [state.focusComposer, state.prompt]);

  return (
    <div className="mx-auto max-w-6xl pt-6 sm:pt-12">
      <div className="text-center">
        <h1 className="text-3xl font-extrabold tracking-tight text-balance sm:text-[42px] sm:leading-[1.15]">
          What would you like to <span className="bg-gradient-to-r from-violet-600 to-indigo-600 bg-clip-text text-transparent">build</span> today?
        </h1>
        <p className="mx-auto mt-3 max-w-2xl text-[15px] text-muted sm:text-base">Turn your idea into a working application with AI — from plan to code, tests, and a verified download.</p>
      </div>

      <div className="mt-10 grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px] xl:items-start">
        <Composer
          key={state.prompt ?? ""}
          initialPrompt={state.prompt ?? ""}
          onCreated={(project, run, startError) =>
            navigate(`/runs/${run.id}`, {
              state: startError ? { startError, projectName: project.name } : undefined,
            })
          }
        />
        <ActivityPanel run={latest?.run ?? null} events={events.data ?? []} projectName={latest?.project.name} />
      </div>
    </div>
  );
}
