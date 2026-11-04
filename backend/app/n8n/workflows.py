"""The registered n8n workflows: the only ones DevPilot may trigger (ARCHITECTURE §8.1).

`config/n8n_workflows.yaml` maps a short key to a webhook path, a description a person reads before approving, the
names of the inputs the workflow accepts and whether it reports back through the callback. GPT names a key and gives
values for exactly those inputs; it never supplies a URL or a path, so nothing outside this file can be reached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PATH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")  # one segment: no slash, no dot, so no traversal
INPUT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_INPUTS = 16


class WorkflowConfigError(ValueError):
    """The workflow file is missing or malformed. With n8n enabled, the app refuses to start."""


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    path: str
    description: str
    inputs: tuple[str, ...]
    callback: bool

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "path": self.path, "description": self.description, "inputs": list(self.inputs), "callback": self.callback}


class WorkflowCatalog:
    def __init__(self, specs: list[WorkflowSpec]) -> None:
        by_key: dict[str, WorkflowSpec] = {}
        paths: set[str] = set()
        for spec in specs:
            if spec.key in by_key:
                raise WorkflowConfigError(f"workflow key {spec.key!r} is listed twice")
            if spec.path in paths:
                raise WorkflowConfigError(f"webhook path {spec.path!r} is listed twice")
            by_key[spec.key] = spec
            paths.add(spec.path)
        self._specs = by_key

    @classmethod
    def from_file(cls, path: Path) -> WorkflowCatalog:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowConfigError(f"n8n workflow file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowConfigError(f"n8n workflow file is not valid YAML: {exc}") from exc
        entries = data.get("workflows") if isinstance(data, dict) else None
        if entries is None:
            entries = {}
        if not isinstance(entries, dict):
            raise WorkflowConfigError("'workflows' must be a mapping of key to workflow")
        return cls([_parse(str(key), raw) for key, raw in entries.items()])

    @property
    def keys(self) -> list[str]:
        return sorted(self._specs)

    def get(self, key: str) -> WorkflowSpec | None:
        return self._specs.get(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._specs[k] for k in self.keys)

    def __len__(self) -> int:
        return len(self._specs)


def _parse(key: str, raw: object) -> WorkflowSpec:
    if not KEY_PATTERN.fullmatch(key):
        raise WorkflowConfigError(f"workflow key {key!r} must match {KEY_PATTERN.pattern}")
    if not isinstance(raw, dict):
        raise WorkflowConfigError(f"workflow {key!r} must be a mapping")
    unknown = set(raw) - {"path", "description", "inputs", "callback"}
    if unknown:
        raise WorkflowConfigError(f"workflow {key!r} has unknown fields: {', '.join(sorted(unknown))}")
    path, description, inputs, callback = raw.get("path"), raw.get("description"), raw.get("inputs", []), raw.get("callback", False)
    if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
        raise WorkflowConfigError(f"workflow {key!r}: path must be one URL segment matching {PATH_PATTERN.pattern}")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 300:
        raise WorkflowConfigError(f"workflow {key!r}: description must be 1 to 300 characters")
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS or not all(isinstance(i, str) and INPUT_PATTERN.fullmatch(i) for i in inputs):
        raise WorkflowConfigError(f"workflow {key!r}: inputs must be at most {MAX_INPUTS} names matching {INPUT_PATTERN.pattern}")
    if len(set(inputs)) != len(inputs):
        raise WorkflowConfigError(f"workflow {key!r}: an input is listed twice")
    if not isinstance(callback, bool):
        raise WorkflowConfigError(f"workflow {key!r}: callback must be true or false")
    return WorkflowSpec(key, path, description.strip(), tuple(inputs), callback)
