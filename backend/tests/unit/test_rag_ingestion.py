"""Document ingestion, offline: each format into located sections, safe refusals, limits, untrusted instructions, and
chunks that keep their source. The documents are invented; no PDF parser is installed, so PDFs use a test double."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from app.drive import fixtures
from app.drive.model import DOCX, GOOGLE_DOC, PDF, TXT, DriveFile
from app.rag.chunking import ChunkingError, chunk_document
from app.rag.extract import ExtractionLimits, ExtractionRefused, ParserUnavailable, extract


def file(kind: str, name: str = "doc", file_id: str = "fxTestFile000001") -> DriveFile:
    return DriveFile(file_id, name, kind, (fixtures.RESEARCH,), "2026-10-01T09:00:00Z", None)


class TwoPages:
    """A PDF test double: what a real parser would return for a two-page PDF."""

    name = "test-double"

    def pages(self, data: bytes) -> list[str]:
        return ["First page about retrieval.", "Second page about citations."]


def test_text_and_google_docs_become_numbered_paragraphs_with_their_metadata() -> None:
    data = b"Title\n\nFirst paragraph.\nStill first.\n\n\nSecond paragraph.\n"
    for kind in (TXT, GOOGLE_DOC):
        doc = extract(file(kind, "Notes"), data, grant_id="grant-0000000000000001")
        assert [(s.location, s.text) for s in doc.sections] == [("paragraph 1", "Title"), ("paragraph 2", "First paragraph. Still first."), ("paragraph 3", "Second paragraph.")]
        assert (doc.file_id, doc.title, doc.modified_time, doc.grant_id) == ("fxTestFile000001", "Notes", "2026-10-01T09:00:00Z", "grant-0000000000000001")
        assert doc.content_hash == hashlib.sha256(data).hexdigest() and doc.parser == "text-paragraphs/1"


def test_a_docx_is_read_by_heading_with_the_standard_library() -> None:
    data = fixtures.docx_bytes([(None, "Intro text."), ("Heading1", "Methods"), (None, "Step one."), (None, "Step two."), ("Heading2", "Metrics"), (None, "Recall at five.")])
    doc = extract(file(DOCX), data, grant_id="g")
    assert [(s.location, s.text) for s in doc.sections] == [("paragraph 1", "Intro text."), ("section 'Methods'", "Step one. Step two."), ("section 'Metrics'", "Recall at five.")]


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return out.getvalue()


@pytest.mark.parametrize(
    ("kind", "data", "message"),
    [
        (DOCX, b"PK\x03\x04 broken", "not a valid archive"),
        (DOCX, _zip({"other.xml": b"<x/>"}), "no document body"),
        (DOCX, _zip({"word/document.xml": b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'}), "DTD or entities"),
        (DOCX, _zip({"word/document.xml": b"<w:document><unclosed>"}), "well-formed"),
        (TXT, b"text\x00with a NUL", "binary"),
        (TXT, b"\xff\xfe\xfa not utf-8", "not UTF-8"),
        (TXT, b"   \n\n  ", "no text"),
        (PDF, b"not a pdf at all", "not a PDF"),
        ("image/png", b"\x89PNG", "not a supported document type"),
    ],
)
def test_malformed_and_unsupported_documents_are_refused_without_quoting_them(kind: str, data: bytes, message: str) -> None:
    with pytest.raises(ExtractionRefused, match=message) as raised:
        extract(file(kind), data, grant_id="g")
    assert "with a NUL" not in str(raised.value)


def test_archive_and_size_limits_hold() -> None:
    many = _zip({f"part{n}.xml": b"<x/>" for n in range(5)} | {"word/document.xml": fixtures.docx_bytes([(None, "x")])})
    with pytest.raises(ExtractionRefused, match="entries"):
        extract(file(DOCX), many, grant_id="g", limits=ExtractionLimits(max_archive_entries=3))
    with pytest.raises(ExtractionRefused, match="expands beyond"):
        extract(file(DOCX), fixtures.docx_bytes([(None, "y" * 5_000)]), grant_id="g", limits=ExtractionLimits(max_uncompressed_bytes=1_000))
    with pytest.raises(ExtractionRefused, match="over the 10 limit"):
        extract(file(TXT), b"far more than ten characters", grant_id="g", limits=ExtractionLimits(max_chars=10))


def test_a_pdf_needs_a_parser_and_none_is_installed() -> None:
    pdf = b"%PDF-1.4\n%%EOF\n"
    with pytest.raises(ParserUnavailable, match="not installed"):
        extract(file(PDF), pdf, grant_id="g")
    doc = extract(file(PDF), pdf, grant_id="g", pdf=TwoPages())  # the interface a real parser would plug into
    assert [s.location for s in doc.sections] == ["page 1", "page 2"]


def test_text_addressed_to_an_ai_agent_is_flagged_and_kept_as_data() -> None:
    poisoned = fixtures.FixtureDrive.research().items[fixtures.POISONED]
    doc = extract(file(TXT), poisoned.content, grant_id="g")
    assert doc.instructions == ("text addressed to AI agents that tries to change their instructions",)
    assert any("Ignore all previous instructions" in s.text for s in doc.sections)  # data, unchanged: never obeyed, never hidden


def test_every_chunk_keeps_its_source_and_overlaps_its_neighbour() -> None:
    text = " ".join(f"Sentence number {n} about chunking and retrieval." for n in range(60))
    doc = extract(file(TXT, "Long notes"), text.encode(), grant_id="grant-0000000000000001")
    chunks = chunk_document(doc, owner="p1", size=300, overlap=60)
    assert len(chunks) > 5 and len({c.id for c in chunks}) == len(chunks)
    assert all((c.owner, c.grant_id, c.file_id, c.title, c.location, c.doc_hash, c.modified_time) == ("p1", "grant-0000000000000001", doc.file_id, "Long notes", "paragraph 1", doc.content_hash, doc.modified_time)
               for c in chunks)  # fmt: skip
    assert all(len(c.text) <= 300 for c in chunks)
    assert all(a.text.split()[-1] in b.text for a, b in zip(chunks, chunks[1:], strict=False))  # the overlap carries words across
    assert [c.id for c in chunk_document(doc, owner="p1", size=300, overlap=60)] == [c.id for c in chunks]  # deterministic


@pytest.mark.parametrize(("size", "overlap"), [(50, 0), (9_000, 0), (400, 300), (400, -1)])
def test_unreasonable_chunk_sizes_are_refused(size: int, overlap: int) -> None:
    doc = extract(file(TXT), b"Some text.", grant_id="g")
    with pytest.raises(ChunkingError):
        chunk_document(doc, owner="p1", size=size, overlap=overlap)
