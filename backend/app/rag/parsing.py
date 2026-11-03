"""The document parsers themselves: bytes in, located sections of text out. **Standard library only, and no app import.**

This file runs in two places. The `document-parser` container copies it next to its entry point
(backend/sandbox_images/document-parser/parse_main.py), where untrusted documents are parsed with no network and strict
limits. The backend imports it only for the simulated fixture documents (app/rag/parsers.py: `FixtureParser`). It never
fetches anything a document refers to, and refuses any DTD or entity in DOCX XML.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPHS = re.compile(r"\n\s*\n")
TEXT_KINDS = frozenset({"txt", "google-doc"})
KINDS = TEXT_KINDS | {"docx"}  # PDF needs a parser that is not installed
DEFAULT_LIMITS = {"max_archive_entries": 200, "max_uncompressed_bytes": 20 * 1024 * 1024, "max_xml_bytes": 10 * 1024 * 1024}


class ParseRefused(Exception):
    """The document cannot be parsed. The message never quotes it."""


def parse_document(kind: str, data: bytes, limits: dict[str, int] | None = None) -> list[tuple[str, str]]:
    """(location, text) for each section, in order."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if kind in TEXT_KINDS:
        return paragraphs(text(data))
    if kind == "docx":
        return docx(data, limits)
    raise ParseRefused(f"{kind} documents are not parsed here")


def text(data: bytes) -> str:
    if b"\x00" in data:
        raise ParseRefused("the file holds binary data, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseRefused("the file is not UTF-8 text") from exc


def paragraphs(content: str) -> list[tuple[str, str]]:
    parts = [" ".join(p.split()) for p in _PARAGRAPHS.split(content.replace("\r\n", "\n"))]
    return [(f"paragraph {n}", p) for n, p in enumerate((p for p in parts if p), start=1)]


def docx(data: bytes, limits: dict[str, int]) -> list[tuple[str, str]]:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseRefused("the DOCX file is not a valid archive") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > limits["max_archive_entries"]:
            raise ParseRefused(f"the DOCX archive has more than {limits['max_archive_entries']} entries")
        if sum(e.file_size for e in entries) > limits["max_uncompressed_bytes"]:
            raise ParseRefused("the DOCX archive expands beyond the size limit")
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise ParseRefused("the DOCX archive has no document body") from exc
        if info.file_size > limits["max_xml_bytes"]:
            raise ParseRefused("the DOCX document body is over the size limit")
        try:
            xml = archive.read(info)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise ParseRefused("the DOCX document body cannot be read") from exc
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ParseRefused("the DOCX document declares a DTD or entities, which are refused")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ParseRefused("the DOCX document body is not well-formed XML") from exc
    sections: list[tuple[str, str]] = []
    heading, buffer, loose = None, [], 0

    def flush() -> None:
        nonlocal loose
        if buffer:
            if heading is None:
                loose += 1
            sections.append((f"section '{heading[:80]}'" if heading else f"paragraph {loose}", " ".join(buffer)))
            buffer.clear()

    for paragraph in root.iter(f"{_W}p"):
        content = " ".join("".join(t.text or "" for t in paragraph.iter(f"{_W}t")).split())
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        if style is not None and str(style.get(f"{_W}val", "")).lower().startswith("heading"):
            flush()
            heading = content or heading
        elif content:
            buffer.append(content)
            if heading is None:
                flush()
    flush()
    return sections
