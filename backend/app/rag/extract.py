"""Text extraction: a document's bytes become located sections of text. Every document is untrusted data.

Supported: TXT and Google Docs (exported as plain text) by paragraph; DOCX with the standard library (zipfile and
ElementTree), by heading, with limits on the archive and a refusal of any DTD or entity; PDF through a
`PdfTextExtractor`, by page. **No PDF parser is installed**, so the default `MissingPdfParser` refuses every PDF and says
so; a parser such as pypdf would plug in behind the same interface, and is not installed here.

A malformed, empty, oversized or unsupported document is refused with a reason that holds none of its text. Text that
tries to instruct an AI agent is reported (`instructions`), and stays data: nothing in a document reaches GPT's
instructions, tool permissions or network policy.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
from xml.etree import ElementTree

from app.drive.model import DriveFile
from app.repos.evaluate import instruction_findings

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPHS = re.compile(r"\n\s*\n")


class ExtractionRefused(Exception):
    """The document cannot be used. The message never quotes it."""


class ParserUnavailable(ExtractionRefused):
    """The format is supported, but the parser it needs is not installed."""


@dataclass(frozen=True)
class Section:
    location: str  # "page 3", "paragraph 2", "section 'Metrics'"
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    file_id: str
    title: str  # untrusted
    kind: str
    modified_time: str
    grant_id: str  # the authorization it was read under
    content_hash: str  # SHA-256 of the bytes read
    sections: tuple[Section, ...]
    instructions: tuple[str, ...]  # text addressed to AI agents: reported, never obeyed
    parser: str = ""  # which extractor read it (SECURITY.md §8 provenance)

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.sections)


@dataclass(frozen=True)
class ExtractionLimits:
    max_chars: int = 500_000
    max_sections: int = 5_000
    max_archive_entries: int = 200
    max_uncompressed_bytes: int = 20 * 1024 * 1024
    max_xml_bytes: int = 10 * 1024 * 1024


class PdfTextExtractor(Protocol):
    name: str

    def pages(self, data: bytes) -> list[str]:
        """Each page's text, in order. Raises ExtractionRefused for a malformed or encrypted PDF."""
        ...


class MissingPdfParser:
    name = "none"

    def pages(self, data: bytes) -> list[str]:
        raise ParserUnavailable("PDF text extraction needs a PDF parser (for example pypdf), which is not installed; nothing was installed")


def extract(file: DriveFile, data: bytes, *, grant_id: str, pdf: PdfTextExtractor | None = None, limits: ExtractionLimits | None = None) -> ExtractedDocument:
    limits = limits or ExtractionLimits()
    kind = file.kind
    if kind in ("txt", "google-doc"):
        sections, parser = _paragraphs(_text(data)), "text-paragraphs/1"
    elif kind == "docx":
        sections, parser = _docx(data, limits), "stdlib-docx/1"
    elif kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ExtractionRefused("the file is not a PDF")
        reader = pdf or MissingPdfParser()
        sections, parser = [Section(f"page {n}", text.strip()) for n, text in enumerate(reader.pages(data), start=1) if text.strip()], f"pdf:{reader.name}"
    else:
        raise ExtractionRefused(f"{file.mime_type} is not a supported document type")
    if not sections:
        raise ExtractionRefused("no text could be extracted")
    if len(sections) > limits.max_sections:
        raise ExtractionRefused(f"the document has more than {limits.max_sections} sections")
    total = sum(len(s.text) for s in sections)
    if total > limits.max_chars:
        raise ExtractionRefused(f"the document has {total} characters, over the {limits.max_chars} limit")
    findings = tuple(dict.fromkeys(f for s in sections for f in instruction_findings(s.text)))
    return ExtractedDocument(file.id, file.name, kind, file.modified_time, grant_id, hashlib.sha256(data).hexdigest(), tuple(sections), findings, parser)


def _text(data: bytes) -> str:
    if b"\x00" in data:
        raise ExtractionRefused("the file holds binary data, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionRefused("the file is not UTF-8 text") from exc


def _paragraphs(text: str) -> list[Section]:
    parts = [" ".join(p.split()) for p in _PARAGRAPHS.split(text.replace("\r\n", "\n"))]
    return [Section(f"paragraph {n}", p) for n, p in enumerate((p for p in parts if p), start=1)]


def _docx(data: bytes, limits: ExtractionLimits) -> list[Section]:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionRefused("the DOCX file is not a valid archive") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > limits.max_archive_entries:
            raise ExtractionRefused(f"the DOCX archive has more than {limits.max_archive_entries} entries")
        if sum(e.file_size for e in entries) > limits.max_uncompressed_bytes:
            raise ExtractionRefused("the DOCX archive expands beyond the size limit")
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise ExtractionRefused("the DOCX archive has no document body") from exc
        if info.file_size > limits.max_xml_bytes:
            raise ExtractionRefused("the DOCX document body is over the size limit")
        try:
            xml = archive.read(info)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise ExtractionRefused("the DOCX document body cannot be read") from exc
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ExtractionRefused("the DOCX document declares a DTD or entities, which are refused")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ExtractionRefused("the DOCX document body is not well-formed XML") from exc
    sections: list[Section] = []
    heading, buffer, loose = None, [], 0

    def flush() -> None:
        nonlocal loose
        if buffer:
            if heading is None:
                loose += 1
            sections.append(Section(f"section '{heading[:80]}'" if heading else f"paragraph {loose}", " ".join(buffer)))
            buffer.clear()

    for paragraph in root.iter(f"{_W}p"):
        text = " ".join("".join(t.text or "" for t in paragraph.iter(f"{_W}t")).split())
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        if style is not None and str(style.get(f"{_W}val", "")).lower().startswith("heading"):
            flush()
            heading = text or heading
        elif text:
            buffer.append(text)
            if heading is None:
                flush()
    flush()
    return sections
