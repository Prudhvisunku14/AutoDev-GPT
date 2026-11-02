"""ToolExecutor: validate -> policy -> invoke -> sanitize (ARCHITECTURE §5.2, Phase 2 subset).

Selection (§9.1), recovery (§9.2) and verification (§9.3) hook in here in later phases.
Persistence of calls is done by the orchestrator loop; this class handles one call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.redaction import Redactor
from app.db.models import RunState
from app.tools.base import ErrorClass, Progress, ToolArgs, ToolContext, ToolError, ToolResult, ToolSpec
from app.tools.policy import Decision, PolicyEngine
from app.tools.registry import ToolRegistry

logger = logging.getLogger("devpilot.tools")

MAX_ERROR_DETAILS = 25


@dataclass(frozen=True)
class PreparedCall:
    spec: ToolSpec
    args: ToolArgs
    decision: Decision
    reason: str


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PolicyEngine, redactor: Redactor, *, max_output_chars: int) -> None:
        self._registry = registry
        self._policy = policy
        self._redactor = redactor
        self._max_output_chars = max_output_chars

    def prepare(self, state: RunState, name: str, raw_arguments: str, *, approved: bool = False) -> PreparedCall | ToolResult:
        """Validate the name, the state and the arguments. Returns a failure result instead of raising."""
        spec = self._registry.get(name)
        if spec is None:
            available = [s.name for s in self._registry.specs_for(state)]
            return ToolResult.failure(
                name, ErrorClass.UNKNOWN_TOOL, f"unknown tool {name!r}", [f"available tools: {', '.join(available) or 'none'}"]
            )
        policy = self._policy.decide(spec, state, approved=approved)
        if policy.decision is Decision.DENY:
            return ToolResult.failure(name, ErrorClass.POLICY_REFUSED, policy.reason)
        try:
            payload = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return ToolResult.failure(name, ErrorClass.INVALID_ARGS, f"arguments are not valid JSON: {exc.msg}")
        try:
            args = spec.args_model.model_validate(payload)
        except ValidationError as exc:
            return ToolResult.failure(
                name, ErrorClass.INVALID_ARGS, f"arguments do not match the {name} schema", _validation_details(exc)
            )
        return PreparedCall(spec, args, policy.decision, policy.reason)

    def invoke(self, ctx: ToolContext, call: PreparedCall) -> ToolResult:
        """Run the handler in a savepoint: a failed tool leaves no partial DB changes.

        An external tool runs all three steps here, inside the caller's transaction. The
        orchestrator loop instead calls begin/run/finish separately, so that no transaction
        is open during the slow step.
        """
        started = time.perf_counter()
        if call.spec.external is not None:
            begun = self.begin_external(ctx, call)
            if isinstance(begun, ToolResult):
                return self._stamp(begun, ctx, started)
            outcome, error = self.run_external(call, begun, ctx.cancel, lambda t, p: self._progress(ctx, t, p))
            return self.finish_external(ctx, call, begun, outcome, error, started=started)
        assert call.spec.handler is not None, "human-input tools are answered, not invoked"
        savepoint = ctx.session.begin_nested()
        try:
            data = call.spec.handler(ctx, call.args)
            savepoint.commit()
            result = ToolResult(ok=True, tool=call.spec.name, data=data)
        except ToolError as exc:
            savepoint.rollback()
            result = ToolResult.failure(call.spec.name, exc.error_class, exc.summary, exc.details)
        except Exception:
            savepoint.rollback()
            logger.exception("tool %s raised", call.spec.name)
            result = ToolResult.failure(call.spec.name, ErrorClass.TOOL_ERROR, "internal error while running the tool")
        return self._stamp(result, ctx, started)

    # --- External tools (slow work outside any transaction) ------------------------

    def begin_external(self, ctx: ToolContext, call: PreparedCall) -> Any:
        """Step 1, inside a transaction. Returns the plan, or a failure ToolResult."""
        assert call.spec.external is not None
        savepoint = ctx.session.begin_nested()
        try:
            plan = call.spec.external.begin(ctx, call.args)
            savepoint.commit()
            return plan
        except ToolError as exc:
            savepoint.rollback()
            if exc.after_rollback is not None:
                exc.after_rollback()
            return ToolResult.failure(call.spec.name, exc.error_class, exc.summary, exc.details)
        except Exception:
            savepoint.rollback()
            logger.exception("tool %s raised while starting", call.spec.name)
            return ToolResult.failure(call.spec.name, ErrorClass.TOOL_ERROR, "internal error while starting the tool")

    def run_external(
        self, call: PreparedCall, plan: Any, cancel: threading.Event | None, progress: Progress
    ) -> tuple[Any, BaseException | None]:
        """Step 2, with no transaction open. Never raises: an exception is handed to `finish_external`."""
        assert call.spec.external is not None
        try:
            return call.spec.external.run(plan, cancel, progress), None
        except Exception as exc:
            if not isinstance(exc, ToolError):
                logger.exception("tool %s raised while running", call.spec.name)
            return None, exc

    def finish_external(
        self,
        ctx: ToolContext,
        call: PreparedCall,
        plan: Any,
        outcome: Any,
        error: BaseException | None,
        *,
        started: float,
    ) -> ToolResult:
        """Step 3, inside a new transaction: record the outcome and build the result."""
        assert call.spec.external is not None
        savepoint = ctx.session.begin_nested()
        try:
            if isinstance(error, ToolError):
                raise error
            if error is not None:
                raise RuntimeError("the external step failed") from error
            data = call.spec.external.finish(ctx, plan, outcome)
            savepoint.commit()
            result = ToolResult(ok=True, tool=call.spec.name, data=data)
        except ToolError as exc:
            savepoint.rollback()
            result = ToolResult.failure(call.spec.name, exc.error_class, exc.summary, exc.details)
        except Exception:
            savepoint.rollback()
            if error is None:
                logger.exception("tool %s raised while finishing", call.spec.name)
            result = ToolResult.failure(call.spec.name, ErrorClass.TOOL_ERROR, "internal error while running the tool")
        return self._stamp(result, ctx, started)

    def _progress(self, ctx: ToolContext, event_type: str, payload: dict[str, Any]) -> None:
        ctx.events.record(ctx.session, project_id=ctx.project.id, run_id=ctx.run.id, type=event_type, payload=payload)

    @staticmethod
    def _stamp(result: ToolResult, ctx: ToolContext, started: float) -> ToolResult:
        result.state = ctx.run.state.value
        result.meta["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    def render(self, result: ToolResult) -> str:
        """Sanitize for GPT and storage: redact secrets, then cap the size."""
        payload: dict[str, Any] = self._redactor.redact(result.model_dump(mode="json", by_alias=True))
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(text) <= self._max_output_chars:
            return text
        payload["data"] = {"truncated": True, "preview": json.dumps(payload.get("data"))[: self._max_output_chars // 2]}
        if payload.get("error") and payload["error"].get("details"):
            payload["error"]["details"] = payload["error"]["details"][:5] + ["... (truncated)"]
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(text) <= self._max_output_chars:
            return text
        # Still too large: keep only the envelope. Never cut through the JSON itself.
        minimal: dict[str, Any] = {k: payload.get(k) for k in ("ok", "tool", "state")}
        minimal["data"] = {"truncated": True}
        if payload.get("error"):
            minimal["error"] = {"class": payload["error"].get("class"), "summary": str(payload["error"].get("summary"))[:500]}
        return json.dumps(minimal, ensure_ascii=False, sort_keys=True)


def _validation_details(exc: ValidationError) -> list[str]:
    # Never echo the input values back: they could be long or contain secrets.
    details = [f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors(include_input=False)]
    if len(details) > MAX_ERROR_DETAILS:
        details = details[:MAX_ERROR_DETAILS] + [f"... and {len(details) - MAX_ERROR_DETAILS} more"]
    return details
