"""Text extraction: a document's bytes become located sections of text. Every document is untrusted data.

The parsing itself is in app/rag/parsing.py, standard library only, so the same code runs in the no-network
`document-parser` container. **This in-process path is for the simulated fixture documents only**: a real document is
parsed through `app/rag/parsers.py`, which refuses it while no approved parser environment exists.

Supported: TXT and Google Docs (exported as plain text) by paragraph; DOCX by heading, with limits on the archive and a
refusal of any DTD or entity; PDF through a `PdfTextExtractor`, by page. **No PDF parser is installed**, so the default
`MissingPdfParser` refuses every PDF and says so.

A malformed, empty, oversized or unsupported document is refused with a reason that holds none of its text. Text that
tries to instruct an AI agent is reported (`instructions`), and stays data: nothing in a document reaches GPT's
instructions, tool permissions or network policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from app.drive.model import DriveFile
from app.rag import parsing
from app.repos.evaluate import instruction_findings


class ExtractionRefused(Exception):
    """The document cannot be used. The message never quotes it."""


class ParserUnavailable(ExtractionRefused):
    """The format is supported, but the parser it needs, or its isolated environment, is not available."""


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
    try:
        if kind in parsing.KINDS:
            parts = parsing.parse_document(kind, data, {"max_archive_entries": limits.max_archive_entries, "max_uncompressed_bytes": limits.max_uncompressed_bytes,
                                                        "max_xml_bytes": limits.max_xml_bytes})  # fmt: skip
            parser = "stdlib-docx/1" if kind == "docx" else "text-paragraphs/1"
        elif kind == "pdf":
            if not data.startswith(b"%PDF-"):
                raise ExtractionRefused("the file is not a PDF")
            reader = pdf or MissingPdfParser()
            parts, parser = [(f"page {n}", t.strip()) for n, t in enumerate(reader.pages(data), start=1) if t.strip()], f"pdf:{reader.name}"
        else:
            raise ExtractionRefused(f"{file.mime_type} is not a supported document type")
    except parsing.ParseRefused as exc:
        raise ExtractionRefused(str(exc)) from exc
    return document(file, data, grant_id=grant_id, sections=[Section(location, text) for location, text in parts], parser=parser, limits=limits)


def document(file: DriveFile, data: bytes, *, grant_id: str, sections: list[Section], parser: str, limits: ExtractionLimits) -> ExtractedDocument:
    """The checks every parser's output passes on the host, wherever it was parsed."""
    if not sections:
        raise ExtractionRefused("no text could be extracted")
    if len(sections) > limits.max_sections:
        raise ExtractionRefused(f"the document has more than {limits.max_sections} sections")
    total = sum(len(s.text) for s in sections)
    if total > limits.max_chars:
        raise ExtractionRefused(f"the document has {total} characters, over the {limits.max_chars} limit")
    findings = tuple(dict.fromkeys(f for s in sections for f in instruction_findings(s.text)))
    return ExtractedDocument(file.id, file.name, file.kind or "", file.modified_time, grant_id, hashlib.sha256(data).hexdigest(), tuple(sections), findings, parser)
