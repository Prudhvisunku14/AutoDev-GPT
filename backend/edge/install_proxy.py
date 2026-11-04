"""The dependency-install proxy: the only way out for a container that installs packages.

It accepts HTTP `CONNECT host:443` and nothing else, for hosts named in its configuration (the egress allowlist's
`sandbox_install` component), and tunnels bytes without reading them (TLS stays end to end between npm and the
registry). Every other request, port or host is refused with 403, and a name that resolves to a private, loopback,
link-local or reserved address is refused too, so the allowlist cannot be turned against the local network. Each
decision is one JSON line in the audit file. Standard library only: it runs in python:3.12-alpine.

    python -I -B install_proxy.py --config proxy.json --listen 0.0.0.0:3128 --audit /var/devpilot/ledger/install-proxy.jsonl
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_HEADER = 8_192
MAX_CONNECTIONS = 64
IDLE_SECONDS = 60.0
CONNECT_TIMEOUT = 15.0


@dataclass(frozen=True)
class ProxyPolicy:
    hosts: frozenset[str]
    max_bytes: int  # per tunnel, both directions together

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProxyPolicy:
        hosts = data.get("hosts")
        if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h and h == h.strip().lower() and ":" not in h and "/" not in h for h in hosts):
            raise ValueError("hosts must be a non-empty list of lower-case host names without ports or paths")
        max_bytes = data.get("max_bytes", 1_000_000_000)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        return cls(frozenset(hosts), max_bytes)


def parse_connect(head: bytes) -> tuple[str, int] | str:
    """(host, port) for a well-formed CONNECT request, or the reason it is refused."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return "the request line is not ASCII"
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return "malformed request line"
    method, target = parts[0], parts[1]
    if method != "CONNECT":
        return f"{method} is not allowed: only CONNECT tunnels to the package registry"
    host, sep, port_text = target.rpartition(":")
    if not sep or not host or not port_text.isdigit():
        return "CONNECT needs host:port"
    return host.lower().rstrip("."), int(port_text)


def decide(policy: ProxyPolicy, host: str, port: int, resolve: Callable[[str], list[str]]) -> tuple[str | None, str]:
    """(address to dial, reason). The address is None when refused."""
    if port != 443:
        return None, f"port {port} is not allowed: only 443"
    if host not in policy.hosts:
        return None, f"{host} is not on the install allowlist"
    try:
        addresses = resolve(host)
    except OSError as exc:
        return None, f"{host} could not be resolved ({type(exc).__name__})"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return None, f"{host} resolves to a non-public address"
    return (addresses[0] if addresses else None), ("allowed" if addresses else f"{host} has no address")


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class InstallProxy:
    def __init__(self, policy: ProxyPolicy, audit: Callable[[dict[str, Any]], None], *, resolve: Callable[[str], list[str]] = _resolve,
                 dial: Callable[[str, int], socket.socket] | None = None) -> None:  # fmt: skip
        self._policy = policy
        self._audit = audit
        self._resolve = resolve
        self._dial = dial or (lambda address, port: socket.create_connection((address, port), timeout=CONNECT_TIMEOUT))

    def handle(self, conn: socket.socket) -> None:
        record: dict[str, Any] = {"at": time.time()}
        try:
            conn.settimeout(IDLE_SECONDS)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEADER:
                    self._refuse(conn, record, "request header too large")
                    return
            parsed = parse_connect(head)
            if isinstance(parsed, str):
                self._refuse(conn, record, parsed)
                return
            host, port = parsed
            record.update(host=host, port=port)
            address, reason = decide(self._policy, host, port, self._resolve)
            if address is None:
                self._refuse(conn, record, reason)
                return
            try:
                upstream = self._dial(address, port)
            except OSError as exc:
                self._refuse(conn, record, f"{host} could not be reached ({type(exc).__name__})", status=b"502 Bad Gateway")
                return
            with upstream:
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                leftover = head.split(b"\r\n\r\n", 1)[1]
                if leftover:
                    upstream.sendall(leftover)
                record["bytes"] = self._pipe(conn, upstream)
                record["decision"] = "tunnelled"
                self._audit(record)
        except OSError as exc:
            record.setdefault("decision", "error")
            record["error"] = type(exc).__name__
            self._audit(record)

    def _refuse(self, conn: socket.socket, record: dict[str, Any], reason: str, *, status: bytes = b"403 Forbidden") -> None:
        record.update(decision="refused", reason=reason)
        self._audit(record)
        try:
            conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + reason.encode("ascii", "replace") + b"\n")
        except OSError:
            pass

    def _pipe(self, a: socket.socket, b: socket.socket) -> int:
        total = 0
        with selectors.DefaultSelector() as sel:
            sel.register(a, selectors.EVENT_READ, b)
            sel.register(b, selectors.EVENT_READ, a)
            while True:
                ready = sel.select(timeout=IDLE_SECONDS)
                if not ready:
                    return total  # idle: close the tunnel
                for key, _ in ready:
                    src: socket.socket = key.fileobj  # type: ignore[assignment]
                    data = src.recv(65_536)
                    if not data:
                        return total
                    total += len(data)
                    if total > self._policy.max_bytes:
                        return total
                    key.data.sendall(data)


def serve(proxy: InstallProxy, listen: tuple[str, int], *, ready: Callable[[socket.socket], None] | None = None, stop: threading.Event | None = None) -> None:
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    with socket.create_server(listen, reuse_port=False) as server:
        server.settimeout(0.5)
        if ready:
            ready(server)
        while not (stop and stop.is_set()):
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            if not slots.acquire(blocking=False):
                conn.close()
                continue

            def run(c: socket.socket = conn) -> None:
                try:
                    with c:
                        proxy.handle(c)
                finally:
                    slots.release()

            threading.Thread(target=run, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--audit", required=True)
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as handle:
        policy = ProxyPolicy.from_dict(json.load(handle))
    lock = threading.Lock()

    def audit(record: dict[str, Any]) -> None:
        with lock, open(args.audit, "a", encoding="utf-8") as out:
            out.write(json.dumps(record, sort_keys=True) + "\n")

    host, _, port = args.listen.rpartition(":")
    print(f"install proxy listening on {args.listen} for {', '.join(sorted(policy.hosts))}", flush=True)
    serve(InstallProxy(policy, audit), (host, int(port)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
