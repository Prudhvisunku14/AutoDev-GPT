"""Accurate tool descriptions derived only from the registered runtime tool specs."""
from __future__ import annotations

from typing import Any


def tool_catalog(registry: Any) -> dict[str, object]:
    output = []
    for name in registry.names:
        spec = registry.get(name)
        if spec is None:
            continue
        output.append({
            "name": name,
            "description": spec.description,
            "risk": spec.risk.value,
            "requires_approval": spec.risk.value in {"EXTERNAL_NETWORK", "GIT_PUSH", "DESTRUCTIVE"},
            "allowed_states": sorted(s.value for s in spec.states) if spec.states is not None else ["non-terminal working states"],
            "kind": "external" if spec.external is not None else "local",
            "registered": True,
            "verified_connectivity": "not established by registration alone",
        })
    return {"tools": output, "total": len(output), "warning": "Registry membership is not proof of live tool connectivity or verified results."}


def compare_tools(registry: Any, first: str, second: str) -> dict[str, object]:
    if first == second:
        raise ValueError("select two distinct tools")
    a, b = registry.get(first), registry.get(second)
    if a is None or b is None:
        raise ValueError("unknown tool; select registered tool names")
    view = tool_catalog(registry)["tools"]
    selected = [item for item in view if item["name"] in (first, second)]
    return {"tools": selected, "difference": {"execution_kind": selected[0]["kind"] != selected[1]["kind"], "risk": selected[0]["risk"] != selected[1]["risk"]}, "recommendation": "Selection requires task requirements and runtime readiness; static registration alone cannot establish which is better."}
