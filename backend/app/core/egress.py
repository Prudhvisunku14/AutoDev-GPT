"""Default-deny outbound network policy (ARCHITECTURE §8.5, SECURITY §9).

Every outbound call names its component and target URL. The call is allowed only
when the host (and port, if listed) appears in that component's allowlist.

Phase 1 scope: the application-layer check. It does not resolve DNS, so a listed
hostname that resolves to a private address is not detected here. Container-level
enforcement arrives in Phase 3.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

_DEFAULT_PORTS = {"https": 443, "http": 80}


class EgressConfigError(ValueError):
    """The allowlist file is missing or malformed. The app refuses to start."""


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class _Entry:
    host: str
    port: int | None  # None: HTTPS on its default port only

    def matches(self, host: str, port: int) -> bool:
        return self.host == host and port == (self.port if self.port is not None else _DEFAULT_PORTS["https"])

    @property
    def is_loopback(self) -> bool:
        ip = _ip_or_none(self.host)
        return ip is not None and ip.is_loopback


class EgressPolicy:
    def __init__(self, allowlist: dict[str, list[str]]) -> None:
        self._entries = {
            component: tuple(_parse_entry(component, raw) for raw in hosts)
            for component, hosts in allowlist.items()
        }

    @classmethod
    def from_file(cls, path: Path) -> EgressPolicy:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EgressConfigError(f"egress allowlist not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise EgressConfigError(f"egress allowlist is not valid YAML: {exc}") from exc
        components = data.get("components") if isinstance(data, dict) else None
        if not isinstance(components, dict) or not components:
            raise EgressConfigError("egress allowlist must define a non-empty 'components' mapping")
        allowlist: dict[str, list[str]] = {}
        for name, hosts in components.items():
            if hosts is None:
                hosts = []
            if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
                raise EgressConfigError(f"component {name!r}: hosts must be a list of strings")
            allowlist[str(name)] = hosts
        return cls(allowlist)

    @property
    def components(self) -> frozenset[str]:
        return frozenset(self._entries)

    def check(self, component: str, url: str) -> EgressDecision:
        entries = self._entries.get(component)
        if entries is None:
            return EgressDecision(False, f"unknown component {component!r}")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            return EgressDecision(False, f"scheme {scheme or '(none)'!r} is not allowed")
        if parts.username or parts.password:
            return EgressDecision(False, "credentials in URLs are not allowed")
        try:
            host = _normalize_host(parts.hostname or "")
            port = parts.port or _DEFAULT_PORTS[scheme]
        except ValueError:
            return EgressDecision(False, "malformed host or port")
        if not host:
            return EgressDecision(False, "URL has no host")

        match = next((e for e in entries if e.matches(host, port)), None)
        if match is None:
            return EgressDecision(False, f"{host}:{port} is not in the {component!r} allowlist")

        ip = _ip_or_none(host)
        if ip is not None and not ip.is_global and not match.is_loopback:
            # Only explicitly listed loopback entries (the n8n port) may be non-global.
            return EgressDecision(False, f"non-public address {host} is not allowed")
        if scheme == "http" and not match.is_loopback:
            return EgressDecision(False, "plain http is allowed only to listed loopback entries")
        return EgressDecision(True, "allowlisted")


def _parse_entry(component: str, raw: str) -> _Entry:
    if "://" in raw or "/" in raw:
        raise EgressConfigError(f"component {component!r}: entry {raw!r} must be host or host:port")
    parts = urlsplit(f"//{raw}")
    try:
        host = _normalize_host(parts.hostname or "")
        port = parts.port
    except ValueError as exc:
        raise EgressConfigError(f"component {component!r}: bad entry {raw!r}") from exc
    if not host:
        raise EgressConfigError(f"component {component!r}: empty host in {raw!r}")
    ip = _ip_or_none(host)
    if ip is not None and not ip.is_global and not ip.is_loopback:
        raise EgressConfigError(f"component {component!r}: private address {raw!r} cannot be listed")
    if ip is not None and ip.is_loopback and port is None:
        raise EgressConfigError(f"component {component!r}: loopback entry {raw!r} needs an explicit port")
    return _Entry(host, port)


def _normalize_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _ip_or_none(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None
