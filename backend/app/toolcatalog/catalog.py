"""The tool catalog: reviewed facts joined with live readiness, and comparisons built only from them.

- `describe()` lists every tool DevPilot really has: its tasks, advantages, limitations, requirements, readiness now, the
  GPT functions registered for it in this backend, and its recorded evidence.
- `compare(names)` puts named tools side by side. A name DevPilot has no adapter for (say, Cloudflare) is reported as
  unknown, with no capabilities or results made up for it.
- `recommend(tasks)` ranks, for each task, the tools that support it: usable before not, then by the strength of evidence.
- `ask(question)` reads which tools and tasks a free-text question names, then compares or recommends.

Nothing here contacts a service, and nothing here calls GPT: GPT reaches the catalog through the `compare_tools` function.
"""

from __future__ import annotations

import re
from typing import Any

from app.toolcatalog.live import Readiness, readiness
from app.toolcatalog.model import LEVELS, CatalogFacts, ToolFacts

_READY_ORDER = {"ready": 0, "simulated": 1, "blocked": 2, "not_configured": 3, "planned": 4}
MAX_COMPARED = 6
# Words that suggest a task in a free-text question. Used only to explain a recommendation from the catalog's own facts;
# the build's stack and tools are chosen by GPT and checked in code, never by these words.
_TASK_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"web ?site|web ?page|homepage|landing|html|css|game|browser", ("code_generation", "browser_verification")),
    (r"\bapp\b|application|program|script|\bcli\b|\bapi\b|service|build|create|generate", ("code_generation", "test_execution")),
    (r"\btests?\b|testing|verify|verification", ("test_generation", "test_execution")),
    (r"bug|fix|debug|error|repair|broken", ("debugging", "code_modification")),
    (r"modify|change|refactor|existing code|update", ("code_modification",)),
    (r"github|repositor|\brepo\b|similar project|open source|pull request|\bpr\b", ("repository_search", "source_reuse")),
    (r"drive|document|\bpdf\b|docx|rag|chatbot|knowledge|citation", ("document_access", "rag_indexing")),
    (r"workflow|automat|notif|email|slack|webhook|schedule|integrat", ("workflow_automation", "notifications")),
    (r"plan|architecture|design|requirement", ("requirement_analysis", "architecture")),
)
_SPLIT = re.compile(r"\s*(?:,|&|/|\bvs\.?|\bversus\b|\band\b|\bor\b|\bwith\b|\bagainst\b|\bto\b)\s*", re.IGNORECASE)
_COMPARE = re.compile(r"\b(?:compare|comparison of|difference between|differences between)\s+(?P<names>.+?)(?:\s+for\b.*)?[?.!]*$", re.IGNORECASE)
_VERSUS = re.compile(r"(?P<a>[\w.\- ]{2,40}?)\s+(?:vs\.?|versus|or|better than)\s+(?P<b>[\w.\- ]{2,40}?)(?:\s+for\b.*)?[?.!]*$", re.IGNORECASE)
_FILLER = re.compile(r"^(?:the|a|an|tools?|platforms?|services?|is|which|should i use|use)\s+|\s+(?:tools?|platforms?|services?)$", re.IGNORECASE)


class ToolCatalog:
    def __init__(self, facts: CatalogFacts) -> None:
        self.facts = facts
        self._state: Any = None
        self._aliases = {alias.lower(): tool.id for tool in facts.tools for alias in (tool.id, tool.name.lower(), *tool.aliases)}
        self._aliases.update({alias.lower(): planned.id for planned in facts.planned for alias in (planned.id, planned.id.replace("_", " "), planned.name.lower())})

    def attach(self, state: Any) -> None:
        """The running app's state, read for readiness and the registered functions. Set once the app is built."""
        self._state = state

    # --- Lookup -----------------------------------------------------------------------------------------------------

    def resolve(self, name: str) -> str | None:
        key = _FILLER.sub("", name.strip().lower()).strip(" .?!'\"")
        return self._aliases.get(key)

    def _readiness(self, tool: ToolFacts) -> Readiness:
        if self._state is None:
            return Readiness("not_configured", "the backend is starting")
        return readiness(tool.id, self._state)

    def _registered(self, tool: ToolFacts) -> list[str]:
        registry = getattr(self._state, "registry", None)
        names = set(registry.names) if registry is not None else set()
        return [function for function in tool.functions if function in names]

    def entry(self, tool: ToolFacts) -> dict[str, Any]:
        strongest = tool.strongest()
        return {
            "id": tool.id, "name": tool.name, "role": tool.role, "description": tool.description,
            "tasks": [{"id": task, "text": self.facts.tasks[task]} for task in tool.tasks],
            "advantages": list(tool.advantages), "limitations": list(tool.limitations), "requirements": list(tool.requirements),
            "readiness": self._readiness(tool).to_dict(),
            "functions": self._registered(tool),
            "evidence": [e.model_dump(mode="json") for e in tool.evidence],
            "strongest_evidence": strongest.model_dump(mode="json") if strongest else None,
            "verified_live": tool.verified_live,
        }  # fmt: skip

    def describe(self) -> dict[str, Any]:
        return {
            "tasks": dict(self.facts.tasks),
            "tools": [self.entry(tool) for tool in self.facts.tools],
            "planned": [{"id": p.id, "name": p.name, "description": p.description, "readiness": {"status": "planned", "detail": "not implemented"}} for p in self.facts.planned],
        }

    # --- Comparison -------------------------------------------------------------------------------------------------

    def compare(self, names: list[str], tasks: list[str] | None = None) -> dict[str, Any]:
        known: list[ToolFacts] = []
        unknown: list[dict[str, str]] = []
        planned: list[dict[str, str]] = []
        for raw in names[:MAX_COMPARED]:
            tool_id = self.resolve(raw)
            tool = self.facts.get(tool_id) if tool_id else None
            plan = next((p for p in self.facts.planned if p.id == tool_id), None)
            if tool is not None:
                if tool not in known:
                    known.append(tool)
            elif plan is not None:
                planned.append({"name": plan.name, "note": f"{plan.name} is planned, not built: {plan.description}"})
            elif raw.strip():
                unknown.append({"name": raw.strip()[:60], "note": f"{raw.strip()[:60]} is not a tool DevPilot has an adapter for, so DevPilot has no tested capabilities, "
                                "readiness or results to compare for it. Its own documentation is the place to check what it offers."})  # fmt: skip
        entries = [self.entry(tool) for tool in known]
        task_sets = {tool.id: set(tool.tasks) for tool in known}
        shared = sorted(set.intersection(*task_sets.values())) if len(task_sets) > 1 else []
        unique = {tool.id: sorted(task_sets[tool.id] - set().union(*(s for k, s in task_sets.items() if k != tool.id))) for tool in known}
        notes = [self._evidence_note(tool, entry) for tool, entry in zip(known, entries, strict=True)]
        if len(known) > 1 and not shared:
            notes.append("These tools do different jobs, so DevPilot uses them together rather than choosing one: " +
                         "; ".join(f"{tool.name} for {', '.join(self.facts.tasks[t].lower() for t in tool.tasks[:2])}" for tool in known))  # fmt: skip
        result: dict[str, Any] = {"tools": entries, "unknown": unknown, "planned": planned, "shared_tasks": shared, "unique_tasks": unique, "notes": notes}
        if tasks:
            result["recommendation"] = self.recommend(tasks, among=[tool.id for tool in known] or None)
        return result

    def _evidence_note(self, tool: ToolFacts, entry: dict[str, Any]) -> str:
        best = tool.strongest()
        state = entry["readiness"]["status"]
        if best is None:
            return f"{tool.name}: no recorded evidence; readiness here is {state}"
        level = {"live": "a live run", "real_container": "real containers with a scripted model", "offline_test": "offline tests only", "documentation": "documentation only"}[best.level]
        return f"{tool.name}: strongest evidence is {level} ({best.outcome}, {best.date}: {best.summary}); readiness here is {state}"

    def recommend(self, tasks: list[str], among: list[str] | None = None) -> dict[str, Any]:
        wanted = [task for task in dict.fromkeys(tasks) if task in self.facts.tasks]
        pool = [tool for tool in self.facts.tools if among is None or tool.id in among]
        picks: list[dict[str, Any]] = []
        gaps: list[str] = []
        for task in wanted:
            able = [tool for tool in pool if task in tool.tasks]
            ranked = sorted(able, key=lambda tool: (_READY_ORDER[self._readiness(tool).status], -LEVELS.index(tool.strongest().level) if tool.strongest() else 0))
            if not ranked:
                gaps.append(task)
                continue
            best = ranked[0]
            status = self._readiness(best).status
            picks.append({"task": task, "text": self.facts.tasks[task], "tool": best.id, "name": best.name, "readiness": status,
                          "alternatives": [tool.id for tool in ranked[1:]],
                          "why": f"{best.name} supports this task" + ("" if status == "ready" else f", but is {status.replace('_', ' ')} on this backend")})  # fmt: skip
        return {"tasks": wanted, "picks": picks, "gaps": [{"task": task, "text": self.facts.tasks[task], "note": "no registered tool supports this task"} for task in gaps]}

    # --- Free-text questions ----------------------------------------------------------------------------------------

    def tasks_in(self, text: str) -> list[str]:
        found: list[str] = []
        for pattern, tasks in _TASK_WORDS:
            if re.search(pattern, text, re.IGNORECASE):
                found.extend(task for task in tasks if task not in found)
        return found

    def names_in(self, question: str) -> list[str]:
        """Tool names a question mentions: every known alias found as a whole word, plus the items of a 'compare A and B'
        or 'A vs B' phrase, so an unknown name is reported rather than dropped."""
        text = question.strip()
        names: list[str] = []
        taken: list[tuple[int, int]] = []  # spans already read as names: an alias inside one is not a second tool
        match = _COMPARE.search(text) or _VERSUS.search(text)
        if match:
            group = "names" if "names" in match.groupdict() else None
            parts = _SPLIT.split(match.group("names")) if group else [match.group("a"), match.group("b")]
            names.extend(part.strip(" .?!'\"") for part in parts if part and part.strip(" .?!'\""))
            taken.append(match.span())
        lowered = text.lower()
        for alias, tool_id in sorted(self._aliases.items(), key=lambda item: -len(item[0])):  # longest first: "opencode via n8n" before "n8n"
            if len(alias) <= 2:
                continue
            for found in re.finditer(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", lowered):
                if any(start < found.end() and found.start() < end for start, end in taken):
                    continue
                taken.append(found.span())
                if not any(self.resolve(name) == tool_id for name in names):
                    names.append(alias)
        return list(dict.fromkeys(names))

    def ask(self, question: str) -> dict[str, Any]:
        names = self.names_in(question)
        tasks = self.tasks_in(question)
        if names:
            return {"question": question, "kind": "comparison", **self.compare(names, tasks or None)}
        return {"question": question, "kind": "recommendation", "recommendation": self.recommend(tasks or ["code_generation", "test_execution"]),
                "note": None if tasks else "The question names no task DevPilot recognises, so this shows the tools for building and testing an application."}  # fmt: skip
