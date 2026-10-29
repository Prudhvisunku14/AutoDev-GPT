"""Strict JSON schemas and per-state tool exposure."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import RunState
from app.tools.base import RiskLevel, ToolArgs, ToolSpec
from app.tools.gates import register_phase2_tools
from app.tools.registry import ToolRegistrationError, ToolRegistry, strict_json_schema

ALLOWED_KEYS = {"type", "description", "enum", "properties", "required", "additionalProperties", "items", "anyOf"}


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    register_phase2_tools(r)
    return r


def _walk(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        found.append((path, node))
        for key, value in node.items():
            if key == "properties":
                for name, prop in value.items():
                    found.extend(_walk(prop, f"{path}.{name}"))
            elif key == "items":
                found.extend(_walk(value, f"{path}[]"))
            elif key == "anyOf":
                for i, option in enumerate(value):
                    found.extend(_walk(option, f"{path}|{i}"))
    return found


def _all_schemas(registry: ToolRegistry) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for state in (RunState.ANALYZING, RunState.PLANNING, RunState.DEFINING_CRITERIA):
        schemas.update({s["name"]: s for s in registry.schemas_for(state)})
    return schemas


@pytest.mark.parametrize("tool", ["submit_analysis", "submit_plan", "submit_criteria", "request_human_input"])
def test_phase2_schemas_are_strict(registry: ToolRegistry, tool: str) -> None:
    schema = _all_schemas(registry)[tool]
    assert schema["type"] == "function"
    assert schema["strict"] is True
    params = schema["parameters"]
    assert params["type"] == "object"
    for path, node in _walk(params):
        assert set(node) <= ALLOWED_KEYS, f"{path} has unsupported keys {set(node) - ALLOWED_KEYS}"
        assert "$ref" not in node and "$defs" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert node["required"] == list(node["properties"]), path


def test_nullable_field_is_required_and_allows_null(registry: ToolRegistry) -> None:
    plan = next(s for s in registry.schemas_for(RunState.PLANNING) if s["name"] == "submit_plan")
    features = plan["parameters"]["properties"]["tasks"]["items"]["properties"]["features"]
    assert "framework" in features["required"]
    assert {"type": "null"} in features["properties"]["framework"]["anyOf"]


def test_literals_become_enums(registry: ToolRegistry) -> None:
    criteria = next(s for s in registry.schemas_for(RunState.DEFINING_CRITERIA) if s["name"] == "submit_criteria")
    item = criteria["parameters"]["properties"]["criteria"]["items"]["properties"]
    assert item["priority"]["enum"] == ["MUST", "SHOULD", "COULD"]
    assert set(item["method"]["enum"]) == {"unit", "api", "e2e", "static", "manual"}


def test_descriptions_survive(registry: ToolRegistry) -> None:
    analysis = next(s for s in registry.schemas_for(RunState.ANALYZING) if s["name"] == "submit_analysis")
    code = analysis["parameters"]["properties"]["requirements"]["items"]["properties"]["code"]
    assert "R-001" in code["description"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RunState.ANALYZING, ["request_human_input", "submit_analysis"]),
        (RunState.PLANNING, ["request_human_input", "submit_plan"]),
        (RunState.DEFINING_CRITERIA, ["request_human_input", "submit_criteria"]),
        (RunState.IMPLEMENTING, ["request_human_input"]),
        (RunState.CREATED, []),
        (RunState.AWAITING_APPROVAL, []),
        (RunState.COMPLETED, []),
        (RunState.CANCELLED, []),
    ],
)
def test_tools_are_exposed_only_in_their_states(registry: ToolRegistry, state: RunState, expected: list[str]) -> None:
    assert [s["name"] for s in registry.schemas_for(state)] == expected


def test_schemas_for_returns_copies(registry: ToolRegistry) -> None:
    first = registry.schemas_for(RunState.ANALYZING)
    first[0]["name"] = "tampered"
    assert registry.schemas_for(RunState.ANALYZING)[0]["name"] == "request_human_input"


class _WithDefault(ToolArgs):
    value: int = 3


class _Loose(BaseModel):
    value: int


class _NestedDefault(ToolArgs):
    inner: list[_WithDefault]


class PublishLike(ToolArgs):
    target: str
    mode: Literal["a", "b"] | None = Field(description="optional value expressed as nullable")


def _spec(name: str, model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description="d", args_model=model, risk=RiskLevel.READ, states=None, handler=lambda c, a: {})  # type: ignore[arg-type]


def test_registration_rejects_bad_tools() -> None:
    r = ToolRegistry()
    r.register(_spec("ok", PublishLike))
    with pytest.raises(ToolRegistrationError, match="already registered"):
        r.register(_spec("ok", PublishLike))
    with pytest.raises(ToolRegistrationError, match="invalid tool name"):
        r.register(_spec("bad name!", PublishLike))
    with pytest.raises(ToolRegistrationError, match="has a default"):
        r.register(_spec("defaults", _WithDefault))
    with pytest.raises(ToolRegistrationError, match="has a default"):
        r.register(_spec("nested", _NestedDefault))
    with pytest.raises(ToolRegistrationError, match="forbid extra"):
        r.register(_spec("loose", _Loose))  # type: ignore[arg-type]
    with pytest.raises(ToolRegistrationError, match="no handler"):
        r.register(ToolSpec(name="nohandler", description="d", args_model=PublishLike, risk=RiskLevel.READ, states=None, handler=None))
    assert r.names == ["ok"]


def test_strict_schema_requires_object_root() -> None:
    class Root(BaseModel):
        model_config = ConfigDict(extra="forbid")
        x: int

    assert strict_json_schema(Root)["required"] == ["x"]
