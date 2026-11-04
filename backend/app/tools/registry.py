"""ToolRegistry: Pydantic models -> OpenAI strict function schemas, exposed per run state.

Strict mode requires, at every object level: `additionalProperties: false` and every
property listed in `required` (optional values are `X | None`). Validation keywords
such as lengths and patterns are kept out of the schema sent to GPT and enforced by
Pydantic instead, so the schema never depends on which keywords a model supports.
"""

from __future__ import annotations

import copy
import re
from typing import Any, get_args

from pydantic import BaseModel

from app.db.models import TERMINAL_RUN_STATES, RunState
from app.tools.base import RiskLevel, ToolSpec

TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Keys allowed in a strict schema node. Everything else is dropped.
_KEEP_KEYS = frozenset({"type", "description", "enum", "properties", "required", "additionalProperties", "items", "anyOf"})


class ToolRegistrationError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(self, spec: ToolSpec) -> None:
        if not TOOL_NAME_PATTERN.fullmatch(spec.name):
            raise ToolRegistrationError(f"invalid tool name {spec.name!r}")
        if spec.name in self._specs:
            raise ToolRegistrationError(f"tool {spec.name!r} is already registered")
        if spec.handler is None and not spec.asks_human and spec.external is None:
            raise ToolRegistrationError(f"tool {spec.name!r} has no handler")
        if spec.handler is not None and spec.external is not None:
            raise ToolRegistrationError(f"tool {spec.name!r} has both a handler and an external step")
        if spec.auto_approve is not None and spec.risk is not RiskLevel.EXTERNAL_NETWORK:
            raise ToolRegistrationError(f"tool {spec.name!r}: only EXTERNAL_NETWORK tools may skip approval, never {spec.risk}")
        _require_all_fields(spec.args_model, spec.name)
        self._schemas[spec.name] = {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": strict_json_schema(spec.args_model),
            "strict": True,
        }
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs_for(self, state: RunState) -> list[ToolSpec]:
        if state in TERMINAL_RUN_STATES or state in (RunState.AWAITING_APPROVAL, RunState.CREATED):
            return []
        return [s for name, s in sorted(self._specs.items()) if s.states is None or state in s.states]

    def schemas_for(self, state: RunState) -> list[dict[str, Any]]:
        return [copy.deepcopy(self._schemas[s.name]) for s in self.specs_for(state)]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    schema = _convert(raw, defs)
    if schema.get("type") != "object":
        raise ToolRegistrationError("tool parameters must be an object")
    return schema


def _convert(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
        if "description" in node:
            target["description"] = node["description"]
        return _convert(target, defs)
    if "const" in node:
        node = {**node, "enum": [node["const"]]}
    out = {k: v for k, v in node.items() if k in _KEEP_KEYS}
    if "anyOf" in out:
        out["anyOf"] = [_convert(option, defs) for option in out["anyOf"]]
    if out.get("type") == "object" or "properties" in out:
        props = {name: _convert(prop, defs) for name, prop in (node.get("properties") or {}).items()}
        out["type"] = "object"
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    if "items" in out:
        out["items"] = _convert(out["items"], defs)
    return out


def _require_all_fields(model: type[BaseModel], tool: str, seen: set[type] | None = None) -> None:
    """Strict mode needs every field required: no defaults anywhere in the argument model."""
    seen = seen or set()
    if model in seen:
        return
    seen.add(model)
    if model.model_config.get("extra") != "forbid":
        raise ToolRegistrationError(f"{tool}: {model.__name__} must forbid extra fields")
    for name, field in model.model_fields.items():
        if not field.is_required():
            raise ToolRegistrationError(f"{tool}: field {model.__name__}.{name} has a default; use `X | None` instead")
        for nested in _nested_models(field.annotation):
            _require_all_fields(nested, tool, seen)


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Models inside an annotation: `Item`, `list[Item]`, `Item | None`, `Annotated[...]`."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in get_args(annotation):
        found.extend(_nested_models(arg))
    return found
