"""Where untrusted documents are parsed (SECURITY.md §8). Two parsers behind one interface:

- `IsolatedParser`, for every real document: the bytes go to the `document-parser` container through the sandbox, which
  creates every job with no network, all capabilities dropped, a read-only root and workspace, a non-root user, and
  memory, CPU, process and time limits (app/sandbox/docker_cli.py). The container runs app/rag/parsing.py and prints
  JSON; the host validates that output strictly (`validate_output`) and applies its own limits again. With no approved
  parser image, no sandbox or no settings, **every document is refused** as `parser_unavailable`. **The image is a
  specification only (backend/sandbox_images/document-parser/): never built, never run, not approved.**
- `FixtureParser`, in-process, for the simulated fixture documents of the tests and the offline demonstration only. A
  `DriveRuntime` that is not simulated refuses it (app/tools/adapters/drive.py), so there is no in-process fallback
  for real documents.

PDF stays unavailable in both until an approved PDF parser exists.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from typing import Protocol

from app.config import Settings
from app.drive.model import DriveFile
from app.rag import parsing
from app.rag.extract import ExtractedDocument, ExtractionLimits, ExtractionRefused, MissingPdfParser, ParserUnavailable, PdfTextExtractor, Section, document, extract
from app.sandbox.policy import Limits, SandboxPolicyError, approved_image, build_job
from app.sandbox.runner import SandboxRunner
from app.sandbox.spec import ExecStatus
from app.tools.adapters.workspace_fs import WorkspaceError, WorkspaceFS

# The parser image's full ID once it is built, run and approved. None: no parser environment exists.
PARSER_IMAGE: str | None = None
PARSER_ENTRY = "/opt/parser/parse_main.py"
REFUSED_EXIT = 2  # parse_main.py's exit code when it refuses a document, with {"refused": reason} on stdout
PARSER_LIMITS = Limits(memory="512m", cpus=1.0, pids=32, timeout_seconds=60.0)  # capped again by the sandbox settings
_LOCATION = re.compile(r"^(?:page \d{1,6}|paragraph \d{1,7}|section '[^'\n]{1,80}')$")
_REFUSAL_CHARS = 200


class DocumentParser(Protocol):
    name: str
    in_process: bool  # True: parses inside the backend, so only for simulated fixture documents

    def parse(self, file: DriveFile, data: bytes, *, grant_id: str) -> ExtractedDocument: ...


class FixtureParser:
    """In-process parsing, for the SIMULATED fixture documents only."""

    name = "in-process (fixture documents only)"
    in_process = True

    def __init__(self, pdf: PdfTextExtractor | None = None, limits: ExtractionLimits | None = None) -> None:
        self._pdf = pdf or MissingPdfParser()
        self._limits = limits or ExtractionLimits()

    def parse(self, file: DriveFile, data: bytes, *, grant_id: str) -> ExtractedDocument:
        return extract(file, data, grant_id=grant_id, pdf=self._pdf, limits=self._limits)


def validate_output(stdout: str, limits: ExtractionLimits) -> list[Section]:
    """The container's answer, accepted only in exactly the agreed shape and within the limits."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise ExtractionRefused("the parser's output is not JSON") from exc
    if isinstance(data, dict) and set(data) == {"refused"} and isinstance(data["refused"], str):
        raise ExtractionRefused("the parser refused the document: " + " ".join(data["refused"].split())[:_REFUSAL_CHARS])
    if not isinstance(data, dict) or set(data) != {"sections"} or not isinstance(data["sections"], list):
        raise ExtractionRefused("the parser's output does not have the agreed shape")
    if len(data["sections"]) > limits.max_sections:
        raise ExtractionRefused(f"the parser returned more than {limits.max_sections} sections")
    sections, total = [], 0
    for item in data["sections"]:
        if not isinstance(item, dict) or set(item) != {"location", "text"} or not isinstance(item["location"], str) or not isinstance(item["text"], str):
            raise ExtractionRefused("a section in the parser's output does not have the agreed shape")
        if not _LOCATION.fullmatch(item["location"]) or not item["text"].strip() or "\x00" in item["text"]:
            raise ExtractionRefused("a section in the parser's output has an invalid location or text")
        total += len(item["text"])
        if total > limits.max_chars:
            raise ExtractionRefused(f"the parser returned more than {limits.max_chars} characters")
        sections.append(Section(item["location"], item["text"]))
    return sections


class IsolatedParser:
    """Parsing in the no-network document-parser container. Refuses every document while that environment is missing."""

    in_process = False

    def __init__(self, sandbox: SandboxRunner | None = None, *, image: str | None = PARSER_IMAGE, settings: Settings | None = None, limits: ExtractionLimits | None = None) -> None:
        self._sandbox = sandbox
        self._image = image
        self._settings = settings
        self._limits = limits or ExtractionLimits()
        self.name = f"container:{image}" if image else "container (no approved image)"

    def unavailable_reason(self) -> str | None:
        if self._image is None:
            return "no document-parser image is built and approved, so untrusted documents are not parsed"
        try:
            approved_image(self._image)
        except SandboxPolicyError:
            return "the document-parser image is not on the sandbox's approved list"
        if self._sandbox is None or self._settings is None:
            return "no sandbox is configured to run the document parser"
        return None

    def parse(self, file: DriveFile, data: bytes, *, grant_id: str) -> ExtractedDocument:
        kind = file.kind
        if kind is None:
            raise ExtractionRefused(f"{file.mime_type} is not a supported document type")
        if kind not in parsing.KINDS:
            raise ParserUnavailable("PDF parsing waits for an approved PDF parser in the document-parser image")
        reason = self.unavailable_reason()
        if reason is not None:
            raise ParserUnavailable(reason)
        assert self._sandbox is not None and self._settings is not None and self._image is not None
        if len(data) > self._settings.workspace_max_file_bytes:
            raise ExtractionRefused("the document is larger than the parser's input limit")
        staging = WorkspaceFS(self._settings.coding_work_dir / "parser", max_file_bytes=self._settings.workspace_max_file_bytes, max_files=2)
        job = secrets.token_hex(16)
        try:
            staging.create_project(job)
            staging.write_bytes(job, "input.bin", data)
            spec = build_job("document-parse", self._image, ("python", "-I", "-B", PARSER_ENTRY, kind, "/workspace/input.bin"), staging.project_dir(job), self._settings, PARSER_LIMITS)
            result = self._sandbox.run(spec)
        except (WorkspaceError, SandboxPolicyError) as exc:
            raise ParserUnavailable(f"the parser job could not be prepared: {exc}") from exc
        finally:
            if staging.has_project(job):
                shutil.rmtree(staging.project_dir(job), ignore_errors=True)
        if result.status in (ExecStatus.UNAVAILABLE, ExecStatus.ERROR, ExecStatus.CANCELLED):
            raise ParserUnavailable(f"the parser environment did not run: {result.detail or result.status.value}")
        if result.status is ExecStatus.FAILED and result.exit_code == REFUSED_EXIT and not result.stdout_truncated:
            validate_output(result.stdout, self._limits)  # the parser's own refusal raises, with its reason
        if result.status is not ExecStatus.SUCCEEDED or result.stdout_truncated:
            raise ExtractionRefused(f"the parser stopped without a usable answer ({result.status.value}{', output cut short' if result.stdout_truncated else ''})")
        return document(file, data, grant_id=grant_id, sections=validate_output(result.stdout, self._limits), parser=self.name, limits=self._limits)
