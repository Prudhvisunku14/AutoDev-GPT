"""n8n over HTTP (ARCHITECTURE §8.1, SECURITY T8). **Tested with a simulated n8n only.**

- `POST /integrations/n8n/callback` (no API token: n8n signs instead). Accepted only with a valid HMAC signature over
  `timestamp.body`, a timestamp within five minutes, a signature not seen before, and a trigger_id that this project's
  run really triggered. Each trigger takes one callback. The result is stored as untrusted, size-limited data.
- `GET /api/v1/integrations/n8n/status` (API token): every registered workflow against n8n's own list (present,
  active, recent executions) and any configuration problem. It reads n8n's public API and changes nothing.

Both answer 404 while n8n is not enabled (N8N_ENABLED=false, the default).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, StringConstraints, ValidationError

from app.core.auth import require_api_token
from app.core.events import event_payload, list_events
from app.db.models import Run
from app.n8n.runtime import CALLBACK_PATH, N8nRuntime
from app.n8n.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureRejected, verify
from app.schemas.api import StrictInput

router = APIRouter(tags=["integrations"])
MAX_CALLBACK_BYTES = 64_000
MAX_RESULT_CHARS = 8_000
_EVENT_LIMIT = 5_000


class CallbackIn(StrictInput):
    trigger_id: Annotated[str, StringConstraints(pattern=r"^trg-[0-9a-f]{16}$")]
    project_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    status: Literal["success", "error"]
    execution_id: Annotated[str, StringConstraints(max_length=64)] | None = None
    result: Any = Field(default=None)


def _runtime(request: Request) -> N8nRuntime:
    runtime = getattr(request.app.state, "n8n", None)
    if runtime is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "n8n is not enabled")
    return runtime


@router.post(CALLBACK_PATH)
async def n8n_callback(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    if not runtime.secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "N8N_WEBHOOK_SECRET is not set, so no callback can be verified")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_CALLBACK_BYTES):
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"a callback is at most {MAX_CALLBACK_BYTES} bytes")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CALLBACK_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"a callback is at most {MAX_CALLBACK_BYTES} bytes")
    try:  # before the body is parsed: an unsigned body is never interpreted
        verify(runtime.secret, request.headers.get(TIMESTAMP_HEADER), request.headers.get(SIGNATURE_HEADER), body, guard=runtime.guard)
    except SignatureRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"callback refused: {exc}") from exc
    try:
        callback = CallbackIn.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the callback body is not a valid DevPilot callback") from exc
    result = json.dumps(callback.result, ensure_ascii=False, default=str)
    truncated = len(result) > MAX_RESULT_CHARS
    with request.app.state.session_factory.begin() as session:
        run = session.get(Run, callback.run_id)
        if run is None or run.project_id != callback.project_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run in this project")
        matching = [e for e in list_events(session, run_id=run.id, limit=_EVENT_LIMIT)
                    if e.type in ("n8n.trigger_started", "n8n.callback") and event_payload(e).get("trigger_id") == callback.trigger_id]  # fmt: skip
        started = next((event_payload(e) for e in matching if e.type == "n8n.trigger_started"), None)
        if started is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "this run made no trigger with that trigger_id")
        if not started.get("callback"):
            raise HTTPException(status.HTTP_409_CONFLICT, "this workflow is not registered to report back")
        if any(e.type == "n8n.callback" for e in matching):
            raise HTTPException(status.HTTP_409_CONFLICT, "this trigger already has its callback")
        request.app.state.events.record(session, project_id=run.project_id, run_id=run.id, type="n8n.callback",
                                        payload={"trigger_id": callback.trigger_id, "workflow": started["workflow"], "status": callback.status,
                                                 "execution_id": callback.execution_id, "result": result[:MAX_RESULT_CHARS], "truncated": truncated})  # fmt: skip
    return {"accepted": True, "trigger_id": callback.trigger_id}


@router.get("/api/v1/integrations/n8n/status", dependencies=[Depends(require_api_token)])
def n8n_status(request: Request) -> dict[str, Any]:
    return _runtime(request).health()
