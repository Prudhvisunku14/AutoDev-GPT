"""Website verification, offline: the install proxy (real sockets on loopback), the verdict, the disposable copy, the Docker
commands (a fake runner) and the self-test's rules. The real image runs in `devpilot setup --web`, not here."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.cli import webruntime
from app.cli.paths import paths as make_paths
from app.core.egress import EgressPolicy
from app.verification import web, web_fixtures
from edge.install_proxy import InstallProxy, ProxyPolicy, decide, parse_connect, serve

POLICY = ProxyPolicy(frozenset({"registry.npmjs.org"}), 10_000)
PUBLIC = lambda host: ["104.16.0.1"]  # noqa: E731


# --- the install proxy ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("head", "expected"), [
    (b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: x\r\n\r\n", ("registry.npmjs.org", 443)),
    (b"CONNECT Registry.NPMJS.org.:443 HTTP/1.1\r\n\r\n", ("registry.npmjs.org", 443)),
    (b"GET http://registry.npmjs.org/ HTTP/1.1\r\n\r\n", "GET is not allowed"),
    (b"CONNECT registry.npmjs.org HTTP/1.1\r\n\r\n", "host:port"),
    (b"CONNECT a b c\r\n\r\n", "malformed"),
])  # fmt: skip
def test_only_well_formed_connect_requests_are_understood(head: bytes, expected: object) -> None:
    got = parse_connect(head)
    assert got == expected if isinstance(expected, tuple) else expected in str(got)


def test_the_proxy_allows_only_listed_hosts_on_443_that_resolve_to_public_addresses() -> None:
    assert decide(POLICY, "registry.npmjs.org", 443, PUBLIC) == ("104.16.0.1", "allowed")
    assert decide(POLICY, "registry.npmjs.org", 80, PUBLIC)[0] is None
    assert decide(POLICY, "github.com", 443, PUBLIC)[0] is None
    for private in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "172.17.0.1"):
        address, reason = decide(POLICY, "registry.npmjs.org", 443, lambda h, a=private: [a])
        assert address is None and "non-public" in reason, private


def test_the_policy_file_is_validated() -> None:
    with pytest.raises(ValueError):
        ProxyPolicy.from_dict({"hosts": ["registry.npmjs.org:443"]})
    with pytest.raises(ValueError):
        ProxyPolicy.from_dict({"hosts": []})


@pytest.fixture
def running_proxy() -> Iterator[tuple[int, list[dict[str, Any]], list[bytes]]]:
    """The real proxy on loopback, dialling a local echo server instead of the internet."""
    echo = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []

    def echo_loop() -> None:
        conn, _ = echo.accept()
        with conn:
            data = conn.recv(1024)
            received.append(data)
            conn.sendall(b"pong:" + data)

    threading.Thread(target=echo_loop, daemon=True).start()
    audit: list[dict[str, Any]] = []
    proxy = InstallProxy(POLICY, audit.append, resolve=PUBLIC, dial=lambda address, port: socket.create_connection(echo.getsockname(), timeout=5))
    ready, stop = threading.Event(), threading.Event()
    port: list[int] = []
    threading.Thread(target=serve, args=(proxy, ("127.0.0.1", 0)), kwargs={"ready": lambda s: (port.append(s.getsockname()[1]), ready.set()), "stop": stop}, daemon=True).start()
    ready.wait(5)
    yield port[0], audit, received
    stop.set()
    echo.close()


def _ask(port: int, head: bytes, then: bytes | None = None) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(head)
        reply = conn.recv(4096)
        if then is not None and reply.startswith(b"HTTP/1.1 200"):
            conn.sendall(then)
            reply += conn.recv(4096)
        return reply


def test_an_allowed_tunnel_carries_bytes_and_is_audited(running_proxy: tuple[int, list[dict[str, Any]], list[bytes]]) -> None:
    port, audit, received = running_proxy
    reply = _ask(port, b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n", b"ping")
    assert reply.startswith(b"HTTP/1.1 200") and b"pong:ping" in reply and received == [b"ping"]
    for _ in range(50):
        if audit:
            break
        threading.Event().wait(0.05)
    assert audit and audit[-1]["decision"] == "tunnelled" and audit[-1]["host"] == "registry.npmjs.org"


def test_anything_else_is_refused_with_403_and_audited(running_proxy: tuple[int, list[dict[str, Any]], list[bytes]]) -> None:
    port, audit, received = running_proxy
    assert _ask(port, b"CONNECT github.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _ask(port, b"GET http://registry.npmjs.org/react HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _ask(port, b"CONNECT registry.npmjs.org:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert received == [] and [a["decision"] for a in audit] == ["refused"] * 3


# --- the verdict ----------------------------------------------------------------------------------------------


def report(stages: list[dict[str, Any]], *, verdict: str = "PASSED", pages: int = 1) -> str:
    return "noise\nDEVPILOT_WEB_REPORT " + json.dumps({"kind": "static-html", "stages": stages, "pages": [{"path": "/"}] * pages, "verdict": verdict, "reason": "r"}) + "\n"


OK_STATIC = [{"name": n, "ok": True, "detail": "fine"} for n in ("serve", "http", "assets")] + [{"name": "browser", "ok": True, "detail": "fine", "ran": True}]


def test_a_pass_needs_every_stage_and_a_browser_that_really_ran() -> None:
    assert web.judge("static-html", report(OK_STATIC)).passed
    no_browser = [s for s in OK_STATIC if s["name"] != "browser"]
    assert web.judge("static-html", report(no_browser)).verdict == "FAILED"
    not_ran = [*no_browser, {"name": "browser", "ok": True, "detail": "x"}]
    assert "did not run" in web.judge("static-html", report(not_ran)).reason
    assert web.judge("static-html", report(OK_STATIC, pages=0)).verdict == "FAILED"
    assert web.judge("static-html", report(OK_STATIC, verdict="FAILED")).verdict == "FAILED"  # the verifier's own word is needed too


def test_a_failing_stage_names_itself_and_no_report_is_not_run() -> None:
    failing = [{"name": "serve", "ok": True, "detail": ""}, {"name": "assets", "ok": False, "detail": "missing: /logo.png (HTTP 404)\nmore"}]
    assert web.judge("static-html", report(failing, verdict="FAILED")).reason == "assets: missing: /logo.png (HTTP 404)"
    assert web.judge("static-html", "killed by OOM").verdict == "NOT_RUN"
    assert web.judge("static-html", "DEVPILOT_WEB_REPORT {broken").verdict == "NOT_RUN"


def test_react_needs_devpilots_own_install() -> None:
    stages = [{"name": n, "ok": True, "detail": ""} for n in web.MANDATORY["react-vite"] if n != "browser"] + [{"name": "browser", "ok": True, "detail": "", "ran": True}]
    assert not web.judge("react-vite", report(stages)).passed
    assert web.judge("react-vite", report(stages), install={"ok": True}).passed
    assert web.judge("react-vite", report(stages), install={"ok": False, "detail": "E404 left-pad"}).reason.startswith("dependency install failed")


# --- the disposable copy --------------------------------------------------------------------------------------


def test_the_copy_leaves_out_package_manager_config_and_build_leftovers_and_refuses_escapes(tmp_path: Path) -> None:
    files = {"site/index.html": b"x", "site/.npmrc": b"//registry/:_authToken=SECRET", "site/node_modules/a/i.js": b"", "site/dist/index.html": b"", "site/src/a.ts": b"", "other/x": b""}
    skipped = web.prepare_copy(files, tmp_path / "copy", component="site")
    assert sorted(p.relative_to(tmp_path / "copy").as_posix() for p in (tmp_path / "copy").rglob("*") if p.is_file()) == ["index.html", "src/a.ts"]
    assert ".npmrc" in skipped
    for bad in ("../x", "/etc/passwd", "a/../../x", "C:/x"):
        with pytest.raises(web.WebVerificationError):
            web.prepare_copy({bad: b""}, tmp_path / f"bad{len(bad)}")


# --- the Docker commands --------------------------------------------------------------------------------------

IMAGE_ID = "sha256:" + "c" * 64


class FakeDocker:
    def __init__(self, *, verify_out: str, install_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.verify_out, self.install_ok = verify_out, install_ok

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = argv[1:]
        self.calls.append(args)
        out = ""
        if args[:2] == ["image", "inspect"]:
            out = IMAGE_ID + "\n"
        elif "install-deps.mjs" in " ".join(args):
            out = "DEVPILOT_INSTALL_REPORT " + json.dumps({"ok": self.install_ok, "detail": "added 60 packages" if self.install_ok else "E404"})
        elif "verify-web.mjs" in " ".join(args):
            out = self.verify_out
        return subprocess.CompletedProcess(argv, 0, out, "")

    def run_args(self, needle: str) -> list[str]:
        return next(c for c in self.calls if c[0] == "run" and needle in " ".join(c))


EGRESS = EgressPolicy({"sandbox_install": ["registry.npmjs.org"]})


@pytest.fixture
def approved() -> Iterator[None]:
    web.approve_locally(IMAGE_ID)
    yield
    web._APPROVED.clear()


def react_report() -> str:
    stages = [{"name": n, "ok": True, "detail": ""} for n in web.MANDATORY["react-vite"] if n != "browser"] + [{"name": "browser", "ok": True, "detail": "", "ran": True}]
    return "DEVPILOT_WEB_REPORT " + json.dumps({"stages": stages, "pages": [{"path": "/"}], "verdict": "PASSED"})


def test_react_installs_through_the_proxy_on_an_internal_network_then_verifies_with_no_network(tmp_path: Path, approved: None) -> None:
    docker = FakeDocker(verify_out=react_report())
    result = web.WebVerifier(EGRESS, run=docker, work_dir=tmp_path).verify(web_fixtures.files(web_fixtures._REACT_OK), "react-vite")
    assert result.passed and result.image_id == IMAGE_ID
    creates = [c for c in docker.calls if c[:2] == ["network", "create"]]
    assert any("--internal" in c for c in creates) and any("--internal" not in c for c in creates)
    proxy = docker.run_args("install_proxy.py")
    assert "python:3.12-alpine" in proxy and "65534:65534" in proxy and proxy[proxy.index("--network") + 1].startswith("devpilot-webegr-")
    install = docker.run_args("install-deps.mjs")
    assert install[install.index("--network") + 1].startswith("devpilot-webinst-") and "HTTPS_PROXY=http://installproxy:3128" in install
    verify = docker.run_args("verify-web.mjs")
    assert verify[verify.index("--network") + 1] == "none"
    for args in (proxy, install, verify):
        assert {"--cap-drop", "ALL", "--read-only", "no-new-privileges", "--pids-limit", "--memory"} <= set(args)
    assert IMAGE_ID in install and IMAGE_ID in verify  # run by exact ID, not by a tag that could be moved
    removed = [c for c in docker.calls if c[:2] == ["rm", "-f"]] + [c for c in docker.calls if c[:2] == ["network", "rm"]]
    assert len(removed) == 5  # three containers and two networks, always
    hosts = json.loads((next(tmp_path.glob("web-*")) / "edge" / "proxy.json").read_text())["hosts"]
    assert hosts == ["registry.npmjs.org"]


def test_a_static_site_needs_no_install_and_no_network_at_all(tmp_path: Path, approved: None) -> None:
    docker = FakeDocker(verify_out=report(OK_STATIC))
    assert web.WebVerifier(EGRESS, run=docker, work_dir=tmp_path).verify(web_fixtures.files(web_fixtures._STATIC_OK), "static-html").passed
    assert not any(c[:2] == ["network", "create"] for c in docker.calls) and not any("install" in " ".join(c) for c in docker.calls if c[0] == "run")


def test_a_failed_install_stops_before_verification_and_still_cleans_up(tmp_path: Path, approved: None) -> None:
    docker = FakeDocker(verify_out=react_report(), install_ok=False)
    result = web.WebVerifier(EGRESS, run=docker, work_dir=tmp_path).verify(web_fixtures.files(web_fixtures._REACT_OK), "react-vite")
    assert result.verdict == "FAILED" and "install failed" in result.reason
    assert not any("verify-web.mjs" in " ".join(c) for c in docker.calls) and sum(c[:2] == ["network", "rm"] for c in docker.calls) == 2


def test_an_image_that_has_not_passed_its_self_test_is_refused(tmp_path: Path) -> None:
    web._APPROVED.clear()
    with pytest.raises(web.WebVerificationError, match="self-test"):
        web.WebVerifier(EGRESS, run=FakeDocker(verify_out=""), work_dir=tmp_path).verify({"index.html": b"x"}, "static-html")


def test_without_the_registry_on_the_allowlist_nothing_is_installed(tmp_path: Path, approved: None) -> None:
    result = web.WebVerifier(EgressPolicy({"sandbox_install": []}), run=FakeDocker(verify_out=""), work_dir=tmp_path).verify(web_fixtures.files(web_fixtures._REACT_OK), "react-vite")
    assert result.verdict == "NOT_RUN" and "registry.npmjs.org" in result.reason
    assert result.cleanup == "removed"


def test_unknown_kinds_are_refused(tmp_path: Path) -> None:
    with pytest.raises(web.WebVerificationError, match="no website verification"):
        web.WebVerifier(EGRESS, run=FakeDocker(verify_out=""), work_dir=tmp_path).verify({}, "next-js")


# --- the self-test and the approval ---------------------------------------------------------------------------


class ScriptedVerifier:
    def __init__(self, verdicts: list[tuple[str, str]], image: str = IMAGE_ID) -> None:
        self.verdicts, self.image = list(verdicts), image

    def verify(self, files: Any, kind: str, **kw: Any) -> web.WebResult:
        verdict, reason = self.verdicts.pop(0)
        return web.WebResult(verdict, reason, kind)

    def image_id(self) -> str:
        return self.image


GOOD = [("PASSED", "every stage passed"), ("PASSED", "every stage passed"), ("FAILED", "assets: missing"), ("FAILED", "typecheck: TS2322")]


def test_the_self_test_requires_passes_and_failures_for_the_right_reasons() -> None:
    assert len(webruntime.self_test(ScriptedVerifier(GOOD), say=lambda s: None)) == 4  # type: ignore[arg-type]
    for bad in ([("FAILED", "browser: x"), *GOOD[1:]], [*GOOD[:2], ("PASSED", "ok"), GOOD[3]], [*GOOD[:3], ("FAILED", "build: something else")], [*GOOD[:2], ("NOT_RUN", "x"), GOOD[3]]):
        with pytest.raises(webruntime.WebRuntimeError):
            webruntime.self_test(ScriptedVerifier(bad), say=lambda s: None)  # type: ignore[arg-type]


def test_an_approval_holds_only_for_the_exact_image_that_passed(tmp_path: Path) -> None:
    web._APPROVED.clear()
    p = make_paths(tmp_path / "home")
    assert webruntime.load_approval(p, ScriptedVerifier([]))[0] is None  # type: ignore[arg-type]
    webruntime.approve(p, IMAGE_ID, [])
    assert webruntime.load_approval(p, ScriptedVerifier([], image="sha256:" + "d" * 64))[0] is None  # type: ignore[arg-type]
    assert IMAGE_ID not in web.approved_image_ids()
    assert webruntime.load_approval(p, ScriptedVerifier([]))[0] == IMAGE_ID and IMAGE_ID in web.approved_image_ids()  # type: ignore[arg-type]
    web._APPROVED.clear()


def test_a_local_folder_is_read_without_descending_into_dependencies_or_build_output(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("x")
    (tmp_path / "index.html").write_text("<h1>")
    for leftover in ("node_modules/react", "dist", ".git/hooks"):
        (tmp_path / leftover).mkdir(parents=True)
        (tmp_path / leftover / "f.js").write_text("y")
    assert sorted(web.read_folder(tmp_path)) == ["index.html", "src/App.tsx"]
    with pytest.raises(web.WebVerificationError, match="over the limit"):
        web.read_folder(tmp_path, max_files=1)
