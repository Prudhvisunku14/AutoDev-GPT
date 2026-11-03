"""The knowledge base: documents in, scoped retrieval and checkable citations out.

- `add` indexes a document, replaces it when its content hash changed, and leaves it alone when it did not.
- `remove`, `revoke` and `sync` take out a deleted document, every document read under a revoked authorization, and
  whatever a fresh listing no longer contains.
- `retrieve` searches only inside an `AccessScope`.
- `answer` is an **extractive test double**: it quotes the retrieved sentences that share the most words with the
  question and cites each one. When nothing relevant is retrieved it says so and cites nothing. It is not a language
  model, and a real application would generate its answer with one.
- `check_citations` proves each citation points at a chunk that was retrieved for this question, still exists in the
  scope, still has the document's current hash, and contains the quoted text. That catches citations to the wrong
  place or to nothing; it does **not** prove an answer is true.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.rag.chunking import Chunk, chunk_document
from app.rag.embeddings import Embedder, EmbeddingUnavailable, estimated_tokens, words
from app.rag.extract import ExtractedDocument
from app.rag.index import AccessScope, InMemoryVectorIndex, VectorIndex

NOT_FOUND = "The authorized documents do not contain an answer to this question."
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Citation:
    file_id: str
    title: str
    location: str
    chunk_id: str
    modified_time: str
    doc_hash: str
    quote: str  # the exact text the answer relies on, from that chunk

    def to_dict(self, *, with_quote: bool = True) -> dict[str, str]:
        data = {"file_id": self.file_id, "title": self.title, "location": self.location, "chunk_id": self.chunk_id, "modified_time": self.modified_time}
        return data | {"quote": self.quote} if with_quote else data


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    citations: tuple[Citation, ...]
    retrieved: tuple[str, ...]  # the chunk IDs retrieved for this question
    grounded: bool  # False: nothing relevant was found, and the answer says so


@dataclass(frozen=True)
class IndexChange:
    file_id: str
    status: str  # added, updated, unchanged
    chunks: int


@dataclass
class KnowledgeBase:
    embedder: Embedder | None
    index: VectorIndex | None = None
    chunk_chars: int = 800
    chunk_overlap: int = 120
    max_embedding_tokens: int = 2_000_000
    _indexed: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # (owner, file ID) -> (content hash, grant ID)

    def __post_init__(self) -> None:
        if self.embedder is not None and self.index is None:
            self.index = InMemoryVectorIndex(self.embedder.name, self.embedder.dimensions)

    def _ready(self) -> tuple[Embedder, VectorIndex]:
        if self.embedder is None or self.index is None:
            raise EmbeddingUnavailable("no embedding provider is configured, so nothing can be indexed or retrieved")
        if self.index.embedder != self.embedder.name:
            raise EmbeddingUnavailable("the index was built by another embedder; its vectors cannot be compared")
        return self.embedder, self.index

    def add(self, document: ExtractedDocument, *, owner: str) -> IndexChange:
        embedder, index = self._ready()
        key = (owner, document.file_id)
        if self._indexed.get(key) == (document.content_hash, document.grant_id):
            return IndexChange(document.file_id, "unchanged", 0)
        chunks = chunk_document(document, owner=owner, size=self.chunk_chars, overlap=self.chunk_overlap)
        tokens = estimated_tokens([c.text for c in chunks])
        if tokens > self.max_embedding_tokens:
            raise BudgetExceeded(f"embedding {document.file_id} needs about {tokens} tokens, over the {self.max_embedding_tokens} limit")
        vectors = embedder.embed([c.text for c in chunks])
        status = "updated" if key in self._indexed else "added"
        index.remove_file(owner, document.file_id)
        index.upsert(chunks, vectors)
        self._indexed[key] = (document.content_hash, document.grant_id)
        return IndexChange(document.file_id, status, len(chunks))

    def remove(self, owner: str, file_id: str) -> int:
        self._indexed.pop((owner, file_id), None)
        return self.index.remove_file(owner, file_id) if self.index is not None else 0

    def revoke(self, grant_id: str) -> int:
        """Every chunk read under this authorization goes, so a revoked grant leaves nothing to retrieve."""
        for key in [k for k, (_, grant) in self._indexed.items() if grant == grant_id]:
            del self._indexed[key]
        return self.index.remove_grant(grant_id) if self.index is not None else 0

    def sync(self, owner: str, scope: AccessScope, current: Mapping[str, str]) -> list[str]:
        """Remove every indexed document that a fresh listing (file ID -> modification time) no longer contains.
        Changed documents are re-read by the caller and given to `add`, which replaces them by hash."""
        if self.index is None:
            return []
        stale = sorted({c.file_id for c in self.index.chunks(scope)} - set(current))
        for file_id in stale:
            self.remove(owner, file_id)
        return stale

    def retrieve(self, question: str, scope: AccessScope, *, k: int, min_score: float) -> list[tuple[Chunk, float]]:
        embedder, index = self._ready()
        return index.search(embedder.embed([question])[0], scope, k=k, min_score=min_score)

    def answer(self, question: str, scope: AccessScope, *, k: int = 4, min_score: float = 0.2, max_quotes: int = 2) -> Answer:
        found = self.retrieve(question, scope, k=k, min_score=min_score)
        wanted = set(words(question))
        candidates = []
        for chunk, score in found:
            for sentence in _SENTENCE.split(chunk.text):
                overlap = len(wanted & set(words(sentence)))
                if overlap:
                    candidates.append((overlap, score, chunk, sentence.strip()))
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2].file_id, item[2].index))
        chosen = candidates[:max_quotes]
        if not chosen:
            return Answer(question, NOT_FOUND, (), tuple(c.id for c, _ in found), False)
        citations = tuple(Citation(c.file_id, c.title, c.location, c.id, c.modified_time, c.doc_hash, sentence) for _, _, c, sentence in chosen)
        text = " ".join(f"{sentence} [{n}]" for n, (_, _, _, sentence) in enumerate(chosen, start=1))
        return Answer(question, text, citations, tuple(c.id for c, _ in found), True)

    def check_citations(self, answer: Answer, scope: AccessScope) -> list[str]:
        """Every way a citation can fail to trace back. Empty means each one is traceable, not that the answer is true."""
        problems = []
        if not answer.grounded and answer.citations:
            problems.append("an answer that found nothing relevant must cite nothing")
        for n, citation in enumerate(answer.citations, start=1):
            chunk = self.index.get(citation.chunk_id, scope) if self.index is not None else None
            if citation.chunk_id not in answer.retrieved:
                problems.append(f"citation {n} points at a chunk that was not retrieved for this question")
            if chunk is None:
                problems.append(f"citation {n} points at a chunk that does not exist in the authorized documents")
                continue
            if chunk.file_id != citation.file_id or chunk.doc_hash != citation.doc_hash:
                problems.append(f"citation {n} names a different or outdated version of its document")
            if citation.quote not in chunk.text:
                problems.append(f"citation {n} quotes text its chunk does not contain")
        return problems

    def documents(self, scope: AccessScope) -> dict[str, dict[str, object]]:
        """The indexed documents in a scope, by file ID: title, chunks and hash. No text."""
        out: dict[str, dict[str, object]] = {}
        for chunk in self.index.chunks(scope) if self.index is not None else []:
            entry = out.setdefault(chunk.file_id, {"title": chunk.title, "chunks": 0, "doc_hash": chunk.doc_hash, "modified_time": chunk.modified_time})
            entry["chunks"] = int(entry["chunks"]) + 1  # type: ignore[call-overload]
        return out
