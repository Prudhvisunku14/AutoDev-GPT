"""A simulated n8n for tests and the offline demonstration. It records every trigger and never opens a socket."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.n8n.client import Execution, N8nRefused, N8nUnavailable, RemoteWorkflow, TriggerAnswer


@dataclass
class FakeN8n:
    workflows_list: list[RemoteWorkflow] = field(default_factory=list)
    executions_by_workflow: dict[str, list[Execution]] = field(default_factory=dict)
    answer: str = '{"message":"Workflow was started"}'
    fail: Exception | None = None  # raised by every call, to simulate an outage or a refusal
    triggered: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)
    name: str = "simulated-n8n"
    simulated: bool = True

    @classmethod
    def with_active(cls, *paths: str) -> FakeN8n:
        return cls([RemoteWorkflow(f"wf{i}", f"DevPilot {p}", True, (p,)) for i, p in enumerate(paths, start=1)])

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def trigger(self, path: str, body: bytes, headers: dict[str, str]) -> TriggerAnswer:
        self._check()
        if not any(path in w.webhook_paths and w.active for w in self.workflows_list):
            raise N8nUnavailable("n8n has no active workflow at this webhook path (404): import and activate it in n8n")
        self.triggered.append((path, body, dict(headers)))
        return TriggerAnswer(200, self.answer)

    def workflows(self) -> list[RemoteWorkflow]:
        self._check()
        return list(self.workflows_list)

    def executions(self, workflow_id: str, *, limit: int) -> list[Execution]:
        self._check()
        return self.executions_by_workflow.get(workflow_id, [])[:limit]


__all__ = ["FakeN8n", "N8nRefused", "N8nUnavailable"]
