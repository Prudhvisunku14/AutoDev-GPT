"""The n8n client: webhook triggers and the public API, behind a Protocol with a fake (app/n8n/fixtures.py).

Every request names the egress allowlist's `n8n` component, follows no redirect and has a size limit. Webhooks are
posted to `<base>/webhook/<path>` with the body and signature the caller prepared; the public API (`/api/v1`, header
`X-N8N-API-KEY`) is read only, to list workflows and their recent executions for the health check. **Tested against a
simulated n8n only; it has never been run against a real n8n instance.**
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from app.core.egress import EgressPolicy

WEBHOOK_NODE = "n8n-nodes-base.webhook"
_MAX_PAGES = 10
_PAGE = 100


class N8nUnavailable(Exception):
    """n8n could not be reached or gave an unusable answer."""


class N8nRefused(Exception):
    """DevPilot refused the request itself: egress policy, a redirect, missing configuration."""


@dataclass(frozen=True)
class TriggerAnswer:
    status_code: int
    body: str  # untrusted text from the workflow, already cut to the size limit


@dataclass(frozen=True)
class RemoteWorkflow:
    id: str
    name: str
    active: bool
    webhook_paths: tuple[str, ...]


@dataclass(frozen=True)
class Execution:
    id: str
    status: str
    mode: str | None
    started_at: str | None
    stopped_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "mode": self.mode, "started_at": self.started_at, "stopped_at": self.stopped_at}


class N8nClient(Protocol):
    name: str
    simulated: bool

    def trigger(self, path: str, body: bytes, headers: dict[str, str]) -> TriggerAnswer: ...

    def workflows(self) -> list[RemoteWorkflow]: ...

    def executions(self, workflow_id: str, *, limit: int) -> list[Execution]: ...


def check_base_url(url: str) -> str:
    """The base URL without a trailing slash. Paths, queries and credentials are refused: DevPilot builds the paths."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("N8N_BASE_URL must be an http or https URL")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("N8N_BASE_URL must be scheme://host:port only")
    return f"{parts.scheme}://{parts.netloc}"


class HttpN8nClient:
    name = "n8n"
    simulated = False

    def __init__(self, base_url: str, egress: EgressPolicy, *, api_key: SecretStr | None, client: httpx.Client | None = None, timeout: float = 15.0,
                 max_bytes: int = 64_000) -> None:  # fmt: skip
        self._base = check_base_url(base_url)
        self._egress = egress
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "DevPilot-AI"})
        self._max = max_bytes

    @property
    def base_url(self) -> str:
        return self._base

    def _send(self, method: str, url: str, *, headers: dict[str, str], content: bytes | None = None) -> tuple[int, bytes]:
        decision = self._egress.check("n8n", url)
        if not decision.allowed:
            raise N8nRefused(f"egress policy refused {httpx.URL(url).host}: {decision.reason}")
        try:
            with self._client.stream(method, url, headers=headers, content=content, follow_redirects=False) as answer:
                if 300 <= answer.status_code < 400:
                    raise N8nRefused(f"n8n answered a redirect ({answer.status_code}); redirects are not followed")
                body = b""
                for chunk in answer.iter_bytes():
                    body += chunk
                    if len(body) > self._max:
                        raise N8nUnavailable(f"the answer is over the {self._max}-byte limit")
                return answer.status_code, body
        except httpx.HTTPError as exc:
            raise N8nUnavailable(f"n8n could not be reached: {type(exc).__name__}") from exc

    def trigger(self, path: str, body: bytes, headers: dict[str, str]) -> TriggerAnswer:
        status, raw = self._send("POST", f"{self._base}/webhook/{quote(path, safe='')}", headers={**headers, "Content-Type": "application/json"}, content=body)
        if status == 404:
            raise N8nUnavailable("n8n has no active workflow at this webhook path (404): import and activate it in n8n")
        if not 200 <= status < 300:
            raise N8nUnavailable(f"the workflow's webhook answered {status}")
        return TriggerAnswer(status, raw.decode("utf-8", errors="replace"))

    def _api(self, path: str) -> dict[str, Any]:
        if self._api_key is None:
            raise N8nRefused("N8N_API_KEY is not set, so the n8n public API cannot be read")
        status, raw = self._send("GET", f"{self._base}/api/v1{path}", headers={"X-N8N-API-KEY": self._api_key.get_secret_value(), "Accept": "application/json"})
        if status in (401, 403):
            raise N8nRefused(f"n8n refused the API key ({status})")
        if status != 200:
            raise N8nUnavailable(f"the n8n public API answered {status}")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise N8nUnavailable("the n8n public API did not answer JSON") from exc
        if not isinstance(data, dict):
            raise N8nUnavailable("the n8n public API answered an unexpected shape")
        return data

    def workflows(self) -> list[RemoteWorkflow]:
        found: list[RemoteWorkflow] = []
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            data = self._api(f"/workflows?limit={_PAGE}" + (f"&cursor={quote(cursor, safe='')}" if cursor else ""))
            found.extend(parse_workflow(w) for w in data.get("data", []) if isinstance(w, dict))
            cursor = data.get("nextCursor") if isinstance(data.get("nextCursor"), str) else None
            if not cursor:
                break
        return found

    def executions(self, workflow_id: str, *, limit: int) -> list[Execution]:
        data = self._api(f"/executions?workflowId={quote(workflow_id, safe='')}&limit={max(1, min(limit, 50))}")
        return [parse_execution(e) for e in data.get("data", []) if isinstance(e, dict)]


def parse_workflow(raw: dict[str, Any]) -> RemoteWorkflow:
    paths = tuple(
        str(node["parameters"]["path"]).strip("/")
        for node in raw.get("nodes") or []
        if isinstance(node, dict) and node.get("type") == WEBHOOK_NODE and isinstance(node.get("parameters"), dict) and node["parameters"].get("path")
    )
    return RemoteWorkflow(str(raw.get("id", "")), str(raw.get("name", ""))[:120], bool(raw.get("active")), paths)


def parse_execution(raw: dict[str, Any]) -> Execution:
    status = raw.get("status")
    if not isinstance(status, str):  # older n8n versions report only `finished`
        status = "success" if raw.get("finished") else "unknown"
    text = lambda key: str(raw[key]) if raw.get(key) is not None else None  # noqa: E731
    return Execution(str(raw.get("id", "")), status, text("mode"), text("startedAt"), text("stoppedAt"))
