"""The vector index, behind one interface. `InMemoryVectorIndex` scores exactly (cosine) and keeps nothing on disk.

Every search names an `AccessScope`, and only chunks inside it are scored at all: another owner's chunks, or chunks read
under an authorization that is not in the scope, can never be returned, however similar they are.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.rag.chunking import Chunk


class VectorIndexError(ValueError):
    pass


@dataclass(frozen=True)
class AccessScope:
    owner: str
    grant_ids: frozenset[str]  # the active authorizations whose documents may be returned


class VectorIndex(Protocol):
    embedder: str

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[tuple[float, ...]]) -> None: ...

    def remove_file(self, owner: str, file_id: str) -> int: ...

    def remove_grant(self, grant_id: str) -> int: ...

    def search(self, vector: tuple[float, ...], scope: AccessScope, *, k: int, min_score: float) -> list[tuple[Chunk, float]]: ...

    def get(self, chunk_id: str, scope: AccessScope) -> Chunk | None: ...

    def chunks(self, scope: AccessScope) -> list[Chunk]: ...


class InMemoryVectorIndex:
    def __init__(self, embedder: str, dimensions: int) -> None:
        self.embedder = embedder  # one embedder per index: vectors from two are not comparable
        self.dimensions = dimensions
        self._rows: dict[str, tuple[Chunk, tuple[float, ...]]] = {}

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[tuple[float, ...]]) -> None:
        if len(chunks) != len(vectors) or any(len(v) != self.dimensions for v in vectors):
            raise VectorIndexError("each chunk needs one vector of the index's dimension")
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._rows[chunk.id] = (chunk, vector)

    def remove_file(self, owner: str, file_id: str) -> int:
        gone = [cid for cid, (c, _) in self._rows.items() if c.owner == owner and c.file_id == file_id]
        for cid in gone:
            del self._rows[cid]
        return len(gone)

    def remove_grant(self, grant_id: str) -> int:
        gone = [cid for cid, (c, _) in self._rows.items() if c.grant_id == grant_id]
        for cid in gone:
            del self._rows[cid]
        return len(gone)

    def _visible(self, chunk: Chunk, scope: AccessScope) -> bool:
        return chunk.owner == scope.owner and chunk.grant_id in scope.grant_ids

    def search(self, vector: tuple[float, ...], scope: AccessScope, *, k: int, min_score: float) -> list[tuple[Chunk, float]]:
        if len(vector) != self.dimensions:
            raise VectorIndexError("the query vector has the wrong dimension")
        scored = [(c, sum(a * b for a, b in zip(vector, v, strict=True))) for c, v in self._rows.values() if self._visible(c, scope)]
        ranked = sorted((pair for pair in scored if pair[1] >= min_score), key=lambda pair: (-pair[1], pair[0].file_id, pair[0].index))
        return ranked[:k]

    def get(self, chunk_id: str, scope: AccessScope) -> Chunk | None:
        row = self._rows.get(chunk_id)
        return row[0] if row is not None and self._visible(row[0], scope) else None

    def chunks(self, scope: AccessScope) -> list[Chunk]:
        return sorted((c for c, _ in self._rows.values() if self._visible(c, scope)), key=lambda c: (c.file_id, c.index))
