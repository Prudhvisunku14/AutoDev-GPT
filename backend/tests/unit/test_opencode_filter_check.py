"""The filter check cannot start work. Proved against the real rules, with no container and no server."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from edge.filter_policy import Policy, Refused, check_body, find_route, parse_head

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "opencode_1_19_5_operations.json"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check() -> ModuleType:
    return _load("devpilot_filter_check", SCRIPTS / "opencode_filter_check.py")


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load("devpilot_filter_probe", SCRIPTS / "opencode_check" / "filter_probe.py")


@pytest.fixture(scope="module")
def policy(check: ModuleType) -> Policy:
    return Policy.from_config(check.filter_config())


def _decide(raw: bytes, body: bytes, policy: Policy) -> None:
    head = parse_head(raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
    route = find_route(head, policy)
    check_body(route.operation, head, body, policy)


def test_every_prompt_the_check_sends_is_one_the_rules_refuse(check: ModuleType, policy: Policy) -> None:
    target = f"/session/{check.INERT_SESSION}/prompt_async?{check.QUERY}"
    for name, payload in check.REFUSED_PROMPTS.items():
        body = json.dumps(payload).encode()
        with pytest.raises(Refused) as caught:
            _decide(check.request_bytes("POST", target, body=body, token="t"), body, policy)
        assert caught.value.status == 403, name


def test_every_session_the_check_refuses_is_refused(check: ModuleType, policy: Policy) -> None:
    for name, payload in check.REFUSED_SESSIONS.items():
        body = json.dumps(payload).encode()
        with pytest.raises(Refused) as caught:
            _decide(check.request_bytes("POST", f"/session?{check.QUERY}", body=body, token="t"), body, policy)
        assert caught.value.status == 403, name


def test_a_valid_prompt_would_pass_so_the_refused_list_really_tests_the_rules(check: ModuleType, policy: Policy) -> None:
    body = json.dumps(check._prompt()).encode()
    _decide(check.request_bytes("POST", f"/session/{check.INERT_SESSION}/prompt_async?{check.QUERY}", body=body, token="t"), body, policy)


def test_the_check_contains_no_acceptable_prompt_on_the_wire(check: ModuleType) -> None:
    source = Path(check.__file__).read_text(encoding="utf-8")
    assert source.count("/prompt_async") == 1  # the one target, used only with REFUSED_PROMPTS
    assert "REFUSED_PROMPTS.items()" in source and "_prompt()" not in source.split("REFUSED_SESSIONS")[1]


def test_the_inert_session_can_never_exist(check: ModuleType) -> None:
    assert re.fullmatch(r"ses_[A-Za-z0-9]+", check.INERT_SESSION)
    assert check.INERT_SESSION.startswith("ses_devpilot")  # the server's own ids are hex time and base62, never words


@pytest.mark.parametrize("list_name", ["UNKNOWN_PATHS", "PATH_TRICKS"])
def test_each_path_the_check_refuses_is_refused(check: ModuleType, policy: Policy, list_name: str) -> None:
    for path in getattr(check, list_name):
        with pytest.raises(Refused) as caught:
            find_route(parse_head(check.request_bytes("GET", path, token="t")), policy)
        assert caught.value.status == 403, path


def test_each_directory_the_check_refuses_is_refused(check: ModuleType, policy: Policy) -> None:
    for query in check.DIRECTORIES:
        with pytest.raises(Refused):
            find_route(parse_head(check.request_bytes("GET", f"/config?{query}", token="t")), policy)


def test_the_sweep_covers_the_whole_server_and_leaves_only_what_devpilot_uses(check: ModuleType, policy: Policy) -> None:
    operations = json.loads(FIXTURE.read_text(encoding="utf-8"))["operations"]
    paths: dict[str, dict[str, Any]] = {}
    for method, path in operations:
        paths.setdefault(path, {})[method.lower()] = {}
    denied, allowed = check.denied_operations({"paths": paths}, policy)
    assert len(denied) == 175 and len(allowed) == 13 and len(denied) + len(allowed) == len(operations)
    methods = {m for m, _ in allowed}
    assert methods == {"GET", "POST"}
    assert not [t for _, t in allowed if re.search(r"/(shell|command|pty|mcp|tui|auth|instance)\b", t)]


def test_the_credentials_never_go_on_a_command_line(check: ModuleType) -> None:
    source = Path(check.__file__).read_text(encoding="utf-8")
    assert 'names=[TOKEN_ENV]' in source  # the filter gets its token by name
    assert "stdin=json.dumps" in source  # requests, with the token in them, go on stdin
    assert not re.search(r"\"--env\",\s*f\"", source)


def test_the_files_given_to_the_container_are_the_real_filter(check: ModuleType) -> None:
    files = check.check_files()
    assert set(files) == {"edge/__init__.py", "edge/filter_policy.py", "edge/filter_server.py", "filter_probe.py", "upstream_witness.py", "filter.json"}
    assert files["edge/filter_policy.py"] == (Path(check.__file__).parents[1] / "edge" / "filter_policy.py").read_bytes()
    config = json.loads(files["filter.json"])
    assert config["upstream_port"] == 4097  # through the witness, never straight to OpenCode
    assert "token" not in json.dumps(config).lower() and "password" not in json.dumps(config).lower()


@pytest.mark.parametrize("port", [80, 443, 4097, 8081, "8080", None])
def test_the_probe_reaches_only_the_filter_and_the_server(probe: ModuleType, port: Any) -> None:
    assert "refused" in probe.run({"port": port, "requests": []})[0]


def test_the_probe_reads_back_only_the_two_logs(probe: ModuleType) -> None:
    assert probe.read_back("/etc/passwd") == ""
    assert probe.read_back("/workspace/x") == ""
    assert probe.READABLE == {"/tmp/filter-audit.log", "/tmp/witness.log"}


def test_the_witness_keeps_only_the_request_line() -> None:
    source = (SCRIPTS / "opencode_check" / "upstream_witness.py").read_text(encoding="utf-8")
    assert 'split(b"\\r\\n", 1)[0]' in source and "log.write(line" in source
    assert source.count("log.write") == 1
