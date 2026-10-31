"""Sends raw bytes to a loopback port and reports what came back. It runs INSIDE the check container.

The filter check must deliver requests no ordinary client would send: encoded paths, conflicting
lengths, unknown methods. A client library would tidy those up, so this writes the bytes as given.
The requests arrive on stdin, because they hold credentials, and the answers name no credential.

It can reach only the filter and the server, on the container's own loopback, and it can read back
only the two log files the check writes. Standard library only, run as `python3 -I`.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
from typing import Any

HOST = "127.0.0.1"
PORTS = frozenset({8080, 4096})
READABLE = frozenset({"/tmp/filter-audit.log", "/tmp/witness.log"})
SECONDS = 10.0
MAX_ANSWER = 64 * 1024
PREVIEW = 400


def exchange(port: int, raw: bytes) -> dict[str, Any]:
    """One connection, one send, everything that comes back."""
    data = bytearray()
    try:
        with socket.create_connection((HOST, port), timeout=SECONDS) as conn:
            conn.sendall(raw)
            conn.shutdown(socket.SHUT_WR)
            while len(data) < MAX_ANSWER:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                data.extend(chunk)
    except OSError as exc:
        if not data:
            return {"error": type(exc).__name__}
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return {"status": status, "headers": sorted(line.split(":", 1)[0].lower() for line in lines[1:] if ":" in line),
            "body": body[:PREVIEW].decode("utf-8", "replace")}  # fmt: skip


def run(request: Any) -> list[dict[str, Any]]:
    port = request.get("port") if isinstance(request, dict) else None
    if port not in PORTS:
        return [{"refused": f"port {port!r} is not one this probe may reach"}]
    return [{"id": item["id"], **exchange(port, base64.b64decode(item["raw_b64"]))} for item in request["requests"]]


def read_back(path: str) -> str:
    if path not in READABLE:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--read":
        sys.stdout.write(read_back(sys.argv[2]))
        return 0
    sys.stdout.write(json.dumps(run(json.load(sys.stdin))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
