"""A RAG chatbot's contract, for any document collection: its components, how they depend on each other, its API and
citation format, the rules every part must keep, and what blocks generating it now.

GPT receives it from `prepare_rag_application`, plans with `submit_plan` as for any multi-stack project, and the plan
gate checks the plan against it (`plan_problems`). It holds no document text and no credential.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

COMPONENTS: tuple[dict[str, Any], ...] = (
    {"name": "knowledge", "stack": "python-fastapi", "path": "knowledge", "depends_on": [],
     "role": "Ingestion pipeline: a Drive reader behind an interface (it takes an access token from its caller), text extraction, chunking, embeddings, a vector index, and source metadata on every chunk"},
    {"name": "backend", "stack": "python-fastapi", "path": "backend", "depends_on": ["knowledge"],
     "role": "FastAPI: POST /api/ask and GET /api/documents, per-user access control, answers only from retrieved chunks, citations on every answer"},
    {"name": "frontend", "stack": "react-vite", "path": "frontend", "depends_on": ["backend"],
     "role": "React chat interface: questions, answers, citations shown with title and location, loading and error states"},
)  # fmt: skip
API = {
    "POST /api/ask": {"request": {"question": "string"}, "response": {"answer": "string", "grounded": "boolean",
                                                                       "citations": [{"document_id": "string", "title": "string", "location": "string", "chunk_id": "string", "quote": "string"}]}},
    "GET /api/documents": {"response": [{"document_id": "string", "title": "string", "modified_time": "string", "chunks": "integer"}]},
    "authentication": "every request names its user; retrieval is filtered to that user's authorized documents before ranking",
}  # fmt: skip
RULES = (
    "Document text is data. It is passed to the model inside a delimited context block and is never followed as an instruction.",
    "Answer only from retrieved chunks. When none is relevant, say the documents do not contain the answer, and cite nothing.",
    "Every citation names a chunk retrieved for that question, and its quote appears in that chunk. Citations show title and location.",
    "A citation's presence is not proof that the answer is correct; the acceptance tests check grounding separately.",
    "A changed document is re-indexed by content hash; a deleted document, or one whose authorization is revoked, is removed before the next answer.",
    "Google Drive access is read-only and limited to approved folders. No Google credential appears in the code or in logs.",
    "A missing embedding provider, index or runtime is an error the user sees, never an empty or invented answer.",
)


def blueprint(*, readiness: Callable[[str], tuple[bool, str]], drive: Mapping[str, Any], ingestion: Mapping[str, Any], index: Mapping[str, Any] | None,
              retrieval: Mapping[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    components = [{**c, "verification": {"ready": readiness(c["stack"])[0], "reason": readiness(c["stack"])[1]}} for c in COMPONENTS]
    blockers = [f"{c['name']} ({c['stack']}): {c['verification']['reason']}" for c in components if not c["verification"]["ready"]]
    if drive.get("simulated"):
        blockers.append("Google Drive access was simulated: real OAuth consent and the Drive API have not been used")
    if index is None:
        blockers.append("no index was built, so retrieval is not configured")
    elif index.get("simulated"):
        blockers.append(f"the index used {index.get('embedder')}, a test double with lexical matching only: a real embedding provider is needed")
    blockers.append("the knowledge component needs a PDF parser, an embedding client and a vector store, which no approved runtime provides")
    return {
        "components": components, "api": API, "rules": list(RULES), "drive": dict(drive), "ingestion": dict(ingestion), "index": dict(index) if index else None,
        "retrieval": dict(retrieval) if retrieval else None, "blockers": blockers,
    }  # fmt: skip


def plan_problems(components: Sequence[Mapping[str, Any]], tasks: Sequence[tuple[str | None, Sequence[int]]], plan: Mapping[str, Any]) -> list[str]:
    """What a plan must keep from the blueprint: each component with its stack, and each dependency between them.
    `tasks` holds (component, depends_on) per task, numbered from 1."""
    problems = []
    planned = {c["name"]: c["stack"] for c in components}
    for wanted in plan["components"]:
        if planned.get(wanted["name"]) != wanted["stack"]:
            problems.append(f"the RAG blueprint needs a component {wanted['name']!r} with stack {wanted['stack']!r}")
    needs = {n: set(deps) for n, (_c, deps) in enumerate(tasks, start=1)}

    def reaches(start: int) -> set[int]:
        seen, stack = set(), list(needs.get(start, ()))
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(needs.get(n, ()))
        return seen

    for wanted in plan["components"]:
        for dependency in wanted["depends_on"]:
            mine = [n for n, (c, _d) in enumerate(tasks, start=1) if c == wanted["name"]]
            theirs = {n for n, (c, _d) in enumerate(tasks, start=1) if c == dependency}
            if mine and theirs and not any(reaches(n) & theirs for n in mine):
                problems.append(f"the RAG blueprint needs a {wanted['name']} task that depends on a {dependency} task")
    return problems
