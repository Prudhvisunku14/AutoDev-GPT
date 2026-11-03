"""Chunking: each section of a document becomes overlapping chunks that keep their source with them.

Every chunk carries its document's ID, title, location, modification time and content hash, the authorization it was
read under, and its owner, so a citation can always be traced back and a search can never reach past its scope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.rag.extract import ExtractedDocument

MIN_CHUNK, MAX_CHUNK = 200, 8_000


class ChunkingError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    id: str
    owner: str  # who may retrieve it (in DevPilot, the project)
    grant_id: str
    file_id: str
    title: str
    location: str
    text: str
    index: int  # its position in the document
    doc_hash: str
    modified_time: str


def chunk_document(document: ExtractedDocument, *, owner: str, size: int = 800, overlap: int = 120) -> list[Chunk]:
    if not MIN_CHUNK <= size <= MAX_CHUNK or not 0 <= overlap <= size // 2:
        raise ChunkingError(f"a chunk is {MIN_CHUNK} to {MAX_CHUNK} characters, and overlaps the next by at most half of that")
    chunks: list[Chunk] = []
    for section in document.sections:
        for text in _windows(section.text, size, overlap):
            n = len(chunks)
            chunk_id = hashlib.sha256(f"{owner}\0{document.file_id}\0{document.content_hash}\0{n}".encode()).hexdigest()[:24]
            chunks.append(Chunk(chunk_id, owner, document.grant_id, document.file_id, document.title, section.location, text, n, document.content_hash, document.modified_time))
    return chunks


def _windows(text: str, size: int, overlap: int) -> list[str]:
    """Windows of at most `size` characters, cut at a space where one is near, each starting `overlap` back."""
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            space = text.rfind(" ", start + size // 2, end)
            end = space if space > start else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        while start < end and text[start - 1] != " ":  # start on a word
            start += 1
    return [w for w in out if w]
