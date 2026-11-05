"""The tool catalog over HTTP: what DevPilot's tools do, whether each is usable here now, and comparisons.

- `GET /api/v1/tools`: every tool with its tasks, advantages, limitations, requirements, readiness and evidence.
- `POST /api/v1/tools/compare`: named tools side by side, a recommendation for tasks, or a free-text question such as
  "Compare n8n and Cloudflare". Answered from the catalog alone: no model is called and nothing is spent.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, StringConstraints

from app.core.auth import require_api_token
from app.schemas.api import StrictInput
from app.toolcatalog.catalog import MAX_COMPARED

router = APIRouter(tags=["tools"], dependencies=[Depends(require_api_token)])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Task = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z_]{2,40}$")]


class CompareIn(StrictInput):
    names: list[Name] | None = Field(default=None, max_length=MAX_COMPARED)
    tasks: list[Task] | None = Field(default=None, max_length=12)
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=500)] | None = None


@router.get("/tools")
def list_tools(request: Request) -> dict[str, Any]:
    return request.app.state.tool_catalog.describe()


@router.post("/tools/compare")
def compare_tools(body: CompareIn, request: Request) -> dict[str, Any]:
    catalog = request.app.state.tool_catalog
    if body.question:
        return catalog.ask(body.question)
    if body.names:
        return {"kind": "comparison", **catalog.compare(body.names, body.tasks)}
    if body.tasks:
        return {"kind": "recommendation", "recommendation": catalog.recommend(body.tasks)}
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give tool names, tasks or a question")
