"""Asking a project's knowledge base: GPT answers from the retrieved chunks, and code checks every citation (app/rag/grounded.py).

- `POST /api/v1/projects/{id}/knowledge/ask` `{question, approved_max_usd}`: retrieves within the project's active Google
  Drive grants only, then makes **one paid GPT call** whose worst case must fit the amount approved for this question.
  An answer whose citations do not check out is replaced by the not-found answer, with the reasons.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, StringConstraints

from app.core.auth import require_api_token
from app.db.models import Project
from app.rag.embeddings import EmbeddingUnavailable
from app.rag.grounded import AnswerRefused, GroundedAnswerer
from app.schemas.api import StrictInput

router = APIRouter(tags=["knowledge"], dependencies=[Depends(require_api_token)])
ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class AskIn(StrictInput):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1_000)]
    approved_max_usd: float = Field(gt=0, le=1.0, description="The most this one answer may cost")
    k: int = Field(default=4, ge=1, le=6)
    min_score: float = Field(default=0.2, ge=0.0, le=1.0)


@router.post("/projects/{project_id}/knowledge/ask")
def ask(project_id: ProjectId, body: AskIn, request: Request) -> dict[str, Any]:
    state = request.app.state
    runtime = state.drive
    if runtime is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Google Drive and its knowledge bases are not enabled in this backend")
    with state.session_factory() as session:
        if session.get(Project, project_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    base = runtime.bases.get(project_id)
    if base is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this project has no knowledge base yet: a run must ingest and index its documents first")
    problem = state.llm_problem()
    if problem is not None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GPT is not configured: {problem}")
    s = state.settings
    answerer = GroundedAnswerer(state.llm_provider(), price_input_per_mtok=s.openai_price_input_per_mtok, price_output_per_mtok=s.openai_price_output_per_mtok,
                                max_output_tokens=s.openai_max_output_tokens)  # fmt: skip
    try:
        retrieved = base.retrieve(body.question, runtime.scope(project_id), k=body.k, min_score=body.min_score)
        answer = answerer.answer(body.question, retrieved, approved_usd=body.approved_max_usd)
    except (AnswerRefused, EmbeddingUnavailable) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    with state.session_factory.begin() as session:
        state.events.record(session, project_id=project_id, type="rag.answered",
                            payload={"grounded": answer.grounded, "citations": [c.chunk_id for c in answer.citations], "retrieved": list(answer.retrieved),
                                     "problems": list(answer.problems), "cost_usd": round(answer.cost_usd, 6), "simulated": state.scripted_llm})  # fmt: skip
    return answer.to_dict() | {"simulated_gpt": state.scripted_llm}
