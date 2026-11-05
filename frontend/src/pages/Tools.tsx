import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, CircleDashed, CircleSlash, FlaskConical, Search, Wrench } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Notice } from "../components/Notice";
import { PageTitle } from "../components/PageTitle";
import { useApi } from "../lib/apiContext";
import { cn } from "../lib/cn";
import { useTools } from "../lib/queries";
import type { ToolAnswer, ToolEntry, ToolEvidence, ToolStatus } from "../lib/types";

// The backend's tool catalog: what each development tool is for, whether it is usable on this backend now, and the evidence
// recorded for it. Questions are answered by the catalog itself, never by a model, so asking costs nothing.

const STATUS: Record<ToolStatus, { label: string; className: string; icon: typeof CheckCircle2 }> = {
  ready: { label: "Ready", className: "bg-emerald-50 text-emerald-700 ring-emerald-200", icon: CheckCircle2 },
  simulated: { label: "Simulated", className: "bg-sky-50 text-sky-700 ring-sky-200", icon: FlaskConical },
  not_configured: { label: "Not set up", className: "bg-slate-100 text-slate-600 ring-slate-200", icon: CircleDashed },
  blocked: { label: "Blocked", className: "bg-amber-50 text-amber-800 ring-amber-200", icon: CircleSlash },
  planned: { label: "Planned", className: "bg-slate-100 text-slate-500 ring-slate-200", icon: CircleDashed },
};

const LEVEL: Record<ToolEvidence["level"], string> = {
  live: "Live run",
  real_container: "Real containers, scripted model",
  offline_test: "Offline tests only",
  documentation: "Documentation only",
};

export function StatusPill({ status }: { status: ToolStatus }) {
  const { label, className, icon: Icon } = STATUS[status];
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1", className)}>
      <Icon className="size-3" aria-hidden />
      {label}
    </span>
  );
}

function taskIds(tool: ToolEntry): string[] {
  return tool.tasks.map((task) => (typeof task === "string" ? task : task.id));
}

function EvidenceLine({ evidence }: { evidence: ToolEvidence | null }) {
  if (!evidence) return <span className="text-muted">No recorded evidence</span>;
  return (
    <span>
      <span className={cn("font-medium", evidence.outcome === "passed" ? "text-emerald-700" : "text-rose-700")}>
        {LEVEL[evidence.level]}, {evidence.outcome}
      </span>{" "}
      <span className="text-muted">
        ({evidence.date}): {evidence.summary}
      </span>
    </span>
  );
}

function ToolCard({ tool }: { tool: ToolEntry }) {
  return (
    <li className="flex flex-col rounded-2xl border border-line bg-white p-5 shadow-soft" aria-label={tool.name}>
      <div className="flex items-start gap-2">
        <Wrench className="mt-0.5 size-4 shrink-0 text-violet-600" aria-hidden />
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold">{tool.name}</h2>
          <p className="text-xs text-muted">{tool.role}</p>
        </div>
        <StatusPill status={tool.readiness.status} />
      </div>
      {tool.description && <p className="mt-2 text-sm leading-relaxed text-slate-700">{tool.description}</p>}
      <p className="mt-2 text-xs leading-relaxed text-muted">{tool.readiness.detail}</p>
      <dl className="mt-3 space-y-2 text-sm">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted">Good at</dt>
          <dd>
            <ul className="list-disc pl-5">
              {tool.advantages.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted">Limitations</dt>
          <dd>
            <ul className="list-disc pl-5">
              {tool.limitations.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-muted">Strongest evidence</dt>
          <dd className="text-[13px]">
            <EvidenceLine evidence={tool.strongest_evidence} />
          </dd>
        </div>
      </dl>
    </li>
  );
}

function Answer({ answer, names }: { answer: ToolAnswer; names: Record<string, string> }) {
  return (
    <section aria-label="Answer" className="space-y-3 rounded-2xl border border-line bg-white p-5 shadow-soft">
      {answer.tools && answer.tools.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-muted">
                <th className="py-1 pr-4 font-medium">Tool</th>
                <th className="py-1 pr-4 font-medium">Here now</th>
                <th className="py-1 pr-4 font-medium">Tasks</th>
                <th className="py-1 font-medium">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {answer.tools.map((tool) => (
                <tr key={tool.id} className="border-t border-line align-top">
                  <td className="py-2 pr-4 font-medium">{tool.name}</td>
                  <td className="py-2 pr-4">
                    <StatusPill status={tool.readiness.status} />
                  </td>
                  <td className="py-2 pr-4 text-[13px]">{taskIds(tool).map((id) => names[id] ?? id).join("; ")}</td>
                  <td className="py-2 text-[13px]">
                    <EvidenceLine evidence={tool.strongest_evidence} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {answer.unknown?.map((item) => (
        <Notice key={item.name} tone="warn" title={`${item.name}: not a DevPilot tool`}>
          {item.note}
        </Notice>
      ))}
      {answer.planned?.map((item) => (
        <Notice key={item.name} title={`${item.name}: planned`}>
          {item.note}
        </Notice>
      ))}
      {answer.notes && answer.notes.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-[13px] text-slate-700">
          {answer.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {answer.recommendation && answer.recommendation.picks.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold">Recommended for this</h3>
          <ul className="mt-1 space-y-1 text-[13px]">
            {answer.recommendation.picks.map((pick) => (
              <li key={pick.task} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{pick.text}:</span> <span>{pick.name}</span> <StatusPill status={pick.readiness} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {answer.recommendation?.gaps.map((gap) => (
        <Notice key={gap.task} tone="warn" title={gap.text}>
          {gap.note}
        </Notice>
      ))}
      {answer.note && <p className="text-[13px] text-muted">{answer.note}</p>}
    </section>
  );
}

export function Tools() {
  const api = useApi();
  const tools = useTools();
  const [question, setQuestion] = useState("");
  const ask = useMutation({ mutationFn: (text: string) => api.askTools(text) });
  const names = tools.data?.tasks ?? {};

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (text.length >= 2) ask.mutate(text);
  };

  return (
    <div className="mx-auto max-w-5xl pt-8">
      <PageTitle title="Tools" subtitle="What each development tool does, whether it is usable on this backend now, and what has really been shown. Asking costs nothing: the catalog answers, not a model." />
      <form onSubmit={submit} className="mb-6 flex gap-2">
        <label htmlFor="tool-question" className="sr-only">
          Ask about tools
        </label>
        <input
          id="tool-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder='For example: "Compare n8n and Cloudflare" or "Which tool is best for a website?"'
          className="min-w-0 flex-1 rounded-xl border border-line bg-white px-4 py-2.5 text-sm shadow-soft outline-none focus:border-violet-300 focus:ring-2 focus:ring-violet-100"
        />
        <button
          type="submit"
          disabled={question.trim().length < 2 || ask.isPending}
          className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-indigo-500 to-violet-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
        >
          <Search className="size-4" aria-hidden />
          Ask
        </button>
      </form>
      {ask.isError && (
        <Notice tone="error" className="mb-6" title="The question could not be answered">
          {ask.error instanceof Error ? ask.error.message : "The backend could not be reached."}
        </Notice>
      )}
      {ask.data && (
        <div className="mb-8">
          <Answer answer={ask.data} names={names} />
        </div>
      )}
      {tools.isError && <Notice tone="error" title="The tool catalog could not be loaded" />}
      {tools.data && (
        <>
          <ul className="grid gap-4 md:grid-cols-2">
            {tools.data.tools.map((tool) => (
              <ToolCard key={tool.id} tool={tool} />
            ))}
          </ul>
          {tools.data.planned.length > 0 && (
            <div className="mt-6 space-y-2">
              {tools.data.planned.map((item) => (
                <Notice key={item.id} title={`${item.name}: planned, not built`}>
                  {item.description}
                </Notice>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
