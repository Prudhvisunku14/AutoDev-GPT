"""The real model gateway with a scripted upstream. For the no-model launcher check only.

It runs inside the gateway container and uses the real `Gateway`, so authentication, the model allowlist,
the budgets and the ledger are the production code. Only `TlsUpstream` is replaced, by an in-memory
object that answers from a fixed script. There is no key, no TLS and no connection to anything, and the
container's only network is internal. Each answer is a fake written for the check: **no model is involved.**

A prompt names its own steps, as `DEVPILOT-STEPS:<base64 JSON list>:END`. The upstream replays them, one per
model turn, so the order cannot depend on OpenCode's other requests. Without steps it uses the script file.

Standard library only. `<mount>/edge/` holds the gateway's own files.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edge.filter_policy import MIN_TOKEN_CHARS  # noqa: E402
from edge.filter_server import JsonLines, build_server  # noqa: E402
from edge.gateway_policy import TOKEN_ENV  # noqa: E402
from edge.gateway_server import Gateway, load_policy  # noqa: E402

STEPS = re.compile(r"DEVPILOT-STEPS:([A-Za-z0-9+/=]+):END")
USAGE = {"input_tokens": 1_000, "output_tokens": 100, "total_tokens": 1_100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}


def _event(payload: dict[str, Any]) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()


def _opening(number: int) -> bytes:
    return _event({"type": "response.created", "response": {"id": f"resp_fake_{number}", "created_at": int(time.time()), "model": "scripted", "service_tier": None}})


def _closing(number: int, usage: dict[str, Any] | None) -> bytes:
    response = {"id": f"resp_fake_{number}", "created_at": int(time.time()), "model": "scripted", "status": "completed", "incomplete_details": None, "service_tier": None, "output": []}
    if usage is not None:
        response["usage"] = usage
    return _event({"type": "response.completed", "response": response})


def text_turn(number: int, text: str, usage: dict[str, Any] | None = USAGE) -> bytes:
    item = f"msg_fake_{number}"
    return b"".join([
        _opening(number),
        _event({"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": item}}),
        _event({"type": "response.output_text.delta", "item_id": item, "content_index": 0, "output_index": 0, "delta": text, "logprobs": []}),
        _event({"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": item}}),
        _closing(number, usage),
    ])  # fmt: skip


def tool_turn(number: int, name: str, arguments: dict[str, Any]) -> bytes:
    item, call = f"fc_fake_{number}", f"call_fake_{number}"
    text = json.dumps(arguments)
    return b"".join([
        _opening(number),
        _event({"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": item, "call_id": call, "name": name, "arguments": ""}}),
        _event({"type": "response.function_call_arguments.delta", "item_id": item, "output_index": 0, "delta": text}),
        _event({"type": "response.output_item.done", "output_index": 0, "item": {"type": "function_call", "id": item, "call_id": call, "name": name, "arguments": text, "status": "completed"}}),
        _closing(number, USAGE),
    ])  # fmt: skip


class Reply:
    def __init__(self, status: int, media: str, body: bytes) -> None:
        self.status = status
        self.media = media
        self._body = body

    def read(self, amount: int) -> bytes:
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk

    def close(self) -> None:
        return None


class Stall(Reply):
    """A model that started and then says nothing, so the session stays busy until it is stopped."""

    def __init__(self, number: int, seconds: float) -> None:
        super().__init__(200, "text/event-stream", _opening(number))
        self._until = time.monotonic() + seconds

    def read(self, amount: int) -> bytes:
        chunk = super().read(amount)
        if chunk:
            return chunk
        while time.monotonic() < self._until:
            time.sleep(0.2)
        return b""


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for item in value.values() for t in _texts(item)]
    if isinstance(value, list):
        return [t for item in value for t in _texts(item)]
    return []


def step_from_prompt(request: dict[str, Any]) -> dict[str, Any] | None:
    """The step for this turn: the n-th of the newest prompt's steps, after n finished tool calls."""
    items = request.get("input")
    if not isinstance(items, list):
        return None
    last_user = max((i for i, item in enumerate(items) if isinstance(item, dict) and item.get("role") == "user"), default=None)
    if last_user is None:
        return None
    found = STEPS.findall("\n".join(_texts(items[last_user])))
    if not found:
        return None
    steps = json.loads(base64.b64decode(found[-1]))
    done = sum(1 for item in items[last_user + 1 :] if isinstance(item, dict) and item.get("type") == "function_call_output")
    return steps[done] if done < len(steps) else {"text": "Done."}


class ScriptedUpstream:
    """Answers each request with its prompt's next step, or the next turn in the script file. OpenCode's
    title request carries no tools, and gets a title."""

    def __init__(self, turns: list[dict[str, Any]], sizes: Any = None) -> None:
        self._turns = list(turns)
        self._lock = threading.Lock()
        self._number = 0
        self._sizes = sizes

    def send(self, body: bytes, *, stream: bool) -> Reply:
        request = json.loads(body)
        if self._sizes is not None:  # how large OpenCode's real requests are: it sizes each reservation
            self._sizes({"bytes": len(body), "items": len(request.get("input") or []), "tools": len(request.get("tools") or [])})
        with self._lock:
            self._number += 1
            number = self._number
            step = step_from_prompt(request) if request.get("tools") else None
            if step is not None:
                turn = step
            elif self._turns and (request.get("tools") or "tools" not in request):
                turn = self._turns.pop(0)
            else:
                turn = {"text": "Done." if request.get("tools") else "Scripted title"}
        if "raise_timeout" in turn:
            raise TimeoutError
        if "status" in turn:
            return Reply(turn["status"], turn.get("media", "application/json"), json.dumps({"error": {"message": "scripted"}}).encode())
        if "stall" in turn:
            return Stall(number, turn["stall"])
        if not stream:
            return Reply(200, "application/json", json.dumps({"id": f"resp_fake_{number}", "output": [], **({} if turn.get("no_usage") else {"usage": turn.get("usage", USAGE)})}).encode())
        if "tool" in turn:
            return Reply(200, "text/event-stream", tool_turn(number, turn["tool"], turn.get("arguments", {})))
        usage = None if turn.get("no_usage") else turn.get("usage", USAGE)
        return Reply(200, "text/event-stream", text_turn(number, turn.get("text", "Done."), usage))


def main() -> int:
    parser = argparse.ArgumentParser(description="The real gateway with a scripted upstream")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8081)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_CHARS:
        print("the gateway did not start: no usable token", file=sys.stderr)
        return 2
    with args.audit.open("a", encoding="utf-8", buffering=1) as audit, args.ledger.open("a", encoding="utf-8", buffering=1) as ledger, (args.ledger.parent / "request-sizes.jsonl").open("a", encoding="utf-8", buffering=1) as sizes:
        upstream = ScriptedUpstream(json.loads(args.script.read_text(encoding="utf-8")), JsonLines(sizes))
        gateway = Gateway(load_policy(args.config), token, upstream, audit=JsonLines(audit), ledger=JsonLines(ledger))  # type: ignore[arg-type]
        with build_server((args.listen_host, args.listen_port), gateway) as server:
            server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
