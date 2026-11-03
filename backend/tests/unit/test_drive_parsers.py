"""Parser isolation (SECURITY.md §8), offline. No container runs: the sandbox is `FakeSandbox`, and its answers are what
the parser would print, produced here from the same parsing code. **The parser image is a specification, never built.**"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import REPO_ROOT, Settings
from app.core.egress import EgressPolicy
from app.drive import fixtures
from app.drive.access import FixtureAuthorizer
from app.drive.model import DOCX, PDF, TXT, DriveFile
from app.drive.source import GoogleDriveSource
from app.rag import parsing
from app.rag.extract import ExtractionRefused, ParserUnavailable
from app.rag.parsers import PARSER_IMAGE, PARSER_LIMITS, REFUSED_EXIT, FixtureParser, IsolatedParser
from app.sandbox.docker_cli import build_create_args
from app.sandbox.fake import FakeSandbox
from app.sandbox.policy import APPROVED_IMAGES, ApprovedImage
from app.sandbox.spec import ExecResult, ExecSpec, ExecStatus
from app.tools.adapters.drive import DriveRuntime
from tests.helpers import make_settings

IMAGE = "sha256:" + "d0" * 32  # a parser image approved inside these tests only
BACKEND = Path(__file__).resolve().parents[2]
DOCX_BYTES = fixtures.docx_bytes([("Heading1", "Methods"), (None, "Step one."), (None, "Step two.")])


def file(kind: str, file_id: str = "fxParseTest00001") -> DriveFile:
    return DriveFile(file_id, "doc", kind, (fixtures.RESEARCH,), "2026-10-01T09:00:00Z", None)


def answers(stdout: str = "", status: ExecStatus = ExecStatus.SUCCEEDED, exit_code: int = 0, truncated: bool = False):  # noqa: ANN201
    specs: list[ExecSpec] = []

    def run(spec: ExecSpec) -> ExecResult:
        specs.append(spec)
        assert (Path(spec.workspace_dir) / "input.bin").is_file()
        return ExecResult(status, spec.command, spec.container_name, exit_code=exit_code, stdout=stdout, stdout_truncated=truncated, cleanup="removed")

    return FakeSandbox(run), specs


def container_output(kind: str, data: bytes) -> str:
    """What parse_main.py prints for this document."""
    return json.dumps({"sections": [{"location": loc, "text": text} for loc, text in parsing.parse_document(kind, data)]})


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def approved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(APPROVED_IMAGES, "document-parser-test", ApprovedImage("document-parser-test", IMAGE))


def test_a_real_runtime_cannot_parse_inside_the_backend() -> None:
    authorizer = FixtureAuthorizer(fixtures.TOKENS, fixtures.RESEARCHER)
    source = GoogleDriveSource(EgressPolicy.from_file(REPO_ROOT / "config" / "egress_allowlist.yaml"), authorizer, client=httpx.Client(transport=fixtures.FixtureDrive.research().transport()))
    with pytest.raises(ValueError, match="only for simulated fixture documents"):
        DriveRuntime(source, authorizer, parser=FixtureParser(), simulated=False)
    assert DriveRuntime(source, authorizer).parser.in_process is False  # the default is the isolated parser


def test_without_an_approved_parser_environment_every_document_is_refused(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert PARSER_IMAGE is None
    for parser, reason in ((IsolatedParser(), "no document-parser image"), (IsolatedParser(image=IMAGE), "not on the sandbox's approved list")):
        with pytest.raises(ParserUnavailable, match=reason):
            parser.parse(file(DOCX), DOCX_BYTES, grant_id="g")
    monkeypatch.setitem(APPROVED_IMAGES, "document-parser-test", ApprovedImage("document-parser-test", IMAGE))
    with pytest.raises(ParserUnavailable, match="no sandbox"):
        IsolatedParser(image=IMAGE).parse(file(DOCX), DOCX_BYTES, grant_id="g")


def test_unsupported_formats_and_pdf_are_refused_before_anything_runs(settings: Settings, approved: None) -> None:
    sandbox, specs = answers()
    parser = IsolatedParser(sandbox, image=IMAGE, settings=settings)
    with pytest.raises(ExtractionRefused, match="not a supported document type"):
        parser.parse(file("image/png"), b"\x89PNG", grant_id="g")
    with pytest.raises(ParserUnavailable, match="approved PDF parser"):
        parser.parse(file(PDF), b"%PDF-1.4", grant_id="g")
    assert specs == []


def test_the_parser_job_runs_isolated_and_limited_and_its_answer_is_rechecked(settings: Settings, approved: None) -> None:
    sandbox, specs = answers(container_output("docx", DOCX_BYTES))
    doc = IsolatedParser(sandbox, image=IMAGE, settings=settings).parse(file(DOCX), DOCX_BYTES, grant_id="grant-0000000000000001")
    spec = specs[0]
    assert spec.command == "document-parse" and spec.image == IMAGE and spec.argv == ("python", "-I", "-B", "/opt/parser/parse_main.py", "docx", "/workspace/input.bin")
    assert (spec.memory, spec.cpus, spec.pids, spec.timeout_seconds) == (PARSER_LIMITS.memory, PARSER_LIMITS.cpus, PARSER_LIMITS.pids, PARSER_LIMITS.timeout_seconds)
    args = build_create_args(spec, settings)
    assert args[args.index("--network") + 1] == "none" and "--read-only" in args and args[args.index("--mount") + 1].endswith(",readonly") and "docker.sock" not in " ".join(args)
    assert Path(spec.workspace_dir).is_relative_to(settings.coding_work_dir / "parser") and not Path(spec.workspace_dir).exists()  # staged, then removed
    assert [(s.location, s.text) for s in doc.sections] == [("section 'Methods'", "Step one. Step two.")] and doc.parser == f"container:{IMAGE}" and len(doc.content_hash) == 64


@pytest.mark.parametrize(
    ("stdout", "status", "exit_code", "truncated", "error", "message"),
    [
        ("not json", ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "not JSON"),
        (json.dumps({"sections": [], "extra": 1}), ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "agreed shape"),
        (json.dumps({"sections": [{"location": "page 1"}]}), ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "agreed shape"),
        (json.dumps({"sections": [{"location": "../../etc/passwd", "text": "x"}]}), ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "invalid location"),
        (json.dumps({"sections": [{"location": "page 1", "text": "a\u0000b"}]}), ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "invalid location or text"),
        (json.dumps({"sections": [{"location": "page 1", "text": "x" * 600_000}]}), ExecStatus.SUCCEEDED, 0, False, ExtractionRefused, "more than 500000 characters"),
        (json.dumps({"refused": "the DOCX file is not a valid archive"}), ExecStatus.FAILED, REFUSED_EXIT, False, ExtractionRefused, "refused the document: the DOCX file"),
        ("Traceback (most recent call last)", ExecStatus.FAILED, 1, False, ExtractionRefused, "without a usable answer"),
        (container_output("docx", DOCX_BYTES), ExecStatus.SUCCEEDED, 0, True, ExtractionRefused, "output cut short"),
        ("", ExecStatus.TIMEOUT, 0, False, ExtractionRefused, "TIMEOUT"),
        ("", ExecStatus.MEMORY_LIMIT, 0, False, ExtractionRefused, "MEMORY_LIMIT"),
        ("", ExecStatus.UNAVAILABLE, 0, False, ParserUnavailable, "did not run"),
    ],
)
def test_the_host_refuses_any_answer_outside_the_agreed_shape(settings: Settings, approved: None, stdout: str, status: ExecStatus, exit_code: int, truncated: bool,
                                                             error: type[Exception], message: str) -> None:  # fmt: skip
    sandbox, _ = answers(stdout, status, exit_code, truncated)
    with pytest.raises(error, match=message):
        IsolatedParser(sandbox, image=IMAGE, settings=settings).parse(file(DOCX), DOCX_BYTES, grant_id="g")


def test_the_container_entry_point_parses_and_refuses_as_agreed(tmp_path: Path) -> None:
    """parse_main.py and parsing.py side by side, as in the image, run with this interpreter over trusted fixtures."""
    image = tmp_path / "opt-parser"
    image.mkdir()
    shutil.copy(BACKEND / "sandbox_images" / "document-parser" / "parse_main.py", image / "parse_main.py")
    shutil.copy(BACKEND / "app" / "rag" / "parsing.py", image / "parsing.py")
    good, bad = tmp_path / "good.bin", tmp_path / "bad.bin"
    good.write_bytes(DOCX_BYTES)
    bad.write_bytes(b"PK\x03\x04 broken")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-B", str(image / "parse_main.py"), *args], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)

    ok = run("docx", str(good))
    assert ok.returncode == 0 and json.loads(ok.stdout) == json.loads(container_output("docx", DOCX_BYTES))
    refused = run("docx", str(bad))
    assert refused.returncode == REFUSED_EXIT and json.loads(refused.stdout) == {"refused": "the DOCX file is not a valid archive"}
    assert run("pdf", str(good)).returncode == REFUSED_EXIT and run("txt").returncode == REFUSED_EXIT


def test_the_shared_parsing_module_needs_nothing_but_the_standard_library() -> None:
    tree = ast.parse((BACKEND / "app" / "rag" / "parsing.py").read_text(encoding="utf-8"))
    imported = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported <= {"__future__", "re", "zipfile", "io", "xml"} and parsing.KINDS == {"txt", "google-doc", "docx"}


def test_the_fixture_parser_keeps_the_offline_behaviour() -> None:
    doc = FixtureParser().parse(file(TXT), b"One.\n\nTwo.", grant_id="g")
    assert [s.location for s in doc.sections] == ["paragraph 1", "paragraph 2"] and doc.parser == "text-paragraphs/1"
    with pytest.raises(ParserUnavailable, match="not installed"):
        FixtureParser().parse(file(PDF), b"%PDF-1.4", grant_id="g")
