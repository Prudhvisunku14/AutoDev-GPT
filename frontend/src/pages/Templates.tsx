import { ArrowRight, LayoutGrid } from "lucide-react";
import { useNavigate } from "react-router";
import { PageTitle } from "../components/PageTitle";

// Starting points for the composer. They are prompts written here, not projects: choosing one only fills the composer.
// The first matches the kind of application the backend can verify today (a Python program with unittest).
export const TEMPLATES = [
  {
    title: "Attendance tracker",
    kind: "Python CLI",
    prompt:
      "Create a Python student attendance tracker that can add students, record daily attendance and calculate attendance percentages. Use only the standard library and include unit tests.",
  },
  {
    title: "Expense splitter",
    kind: "Python CLI",
    prompt: "Create a Python command-line expense splitter: add people and expenses, then show who owes whom and how much, with unit tests.",
  },
  {
    title: "Weather forecast",
    kind: "Python CLI",
    prompt:
      "Create a weather forecast application in Python that reports temperature, humidity and wind speed for a city, using a replaceable weather data provider, with clear errors for unknown cities and unit tests that use a fake provider.",
  },
  {
    title: "Habit tracker",
    kind: "Python CLI",
    prompt: "Create a Python habit tracker that records daily habits and reports current and longest streaks, with unit tests.",
  },
] as const;

export function Templates() {
  const navigate = useNavigate();
  return (
    <div className="mx-auto max-w-4xl pt-8">
      <PageTitle title="Templates" subtitle="Starting points for a requirement. Choosing one fills in the prompt; you can edit it before saving." />
      <ul className="grid gap-4 sm:grid-cols-2">
        {TEMPLATES.map((template) => (
          <li key={template.title} className="flex flex-col rounded-2xl border border-line bg-white p-5 shadow-soft">
            <div className="flex items-center gap-2">
              <LayoutGrid className="size-4 text-violet-600" aria-hidden />
              <h2 className="font-semibold">{template.title}</h2>
              <span className="ml-auto rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600">{template.kind}</span>
            </div>
            <p className="mt-2 flex-1 text-sm leading-relaxed text-muted">{template.prompt}</p>
            <button
              type="button"
              onClick={() => navigate("/", { state: { prompt: template.prompt } })}
              className="mt-4 inline-flex items-center gap-1.5 self-start text-sm font-medium text-violet-700 hover:underline"
            >
              Use this template <ArrowRight className="size-4" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
