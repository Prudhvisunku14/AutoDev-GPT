"""A persistent vector index in SQLite: chunks and their vectors survive restarts, and every read is scoped in SQL.

- **Isolation:** every query filters on the owner (the project) and the grant IDs that are still active, in the WHERE
  clause, and the result is checked once more in Python. A chunk of another project, or of a revoked grant, is never returned.
- **One embedder per index file:** the file records which embedder and dimension built it and refuses any other, because
  vectors from two models cannot be compared.
- **Updates and revocation:** a re-ingested document replaces its chunks (`remove_file`, then `upsert`); revoking a grant
  deletes every chunk read under it.
Vectors are stored L2-normalised as float32 and scored by cosine in Python: right for a project's documents, not millions.
"""

from __future__ import annotations

import array
import math
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.rag.chunking import Chunk
from app.rag.index import AccessScope, VectorIndexError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, grant_id TEXT NOT NULL, file_id TEXT NOT NULL, title TEXT NOT NULL,
    location TEXT NOT NULL, text TEXT NOT NULL, idx INTEGER NOT NULL, doc_hash TEXT NOT NULL, modified_time TEXT NOT NULL, vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_owner_grant ON chunks (owner, grant_id);
CREATE INDEX IF NOT EXISTS chunks_owner_file ON chunks (owner, file_id);
"""


def _normalised(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _pack(vector: Sequence[float]) -> bytes:
    return array.array("f", _normalised(vector)).tobytes()


def _unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


class SqliteVectorIndex:
    def __init__(self, path: Path, embedder: str, dimensions: int) -> None:
        self.embedder = embedder
        self.dimensions = dimensions
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            found = dict(self._db.execute("SELECT key, value FROM meta").fetchall())
            if not found:
                self._db.executemany("INSERT INTO meta VALUES (?, ?)", [("embedder", embedder), ("dimensions", str(dimensions))])
            elif found.get("embedder") != embedder or found.get("dimensions") != str(dimensions):
                self._db.close()
                raise VectorIndexError(f"{path.name} was built by {found.get('embedder')} ({found.get('dimensions')} dimensions); its vectors cannot be compared with {embedder}'s")

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[tuple[float, ...]]) -> None:
        if len(chunks) != len(vectors) or any(len(v) != self.dimensions for v in vectors):
            raise VectorIndexError("each chunk needs one vector of the index's dimension")
        rows = [(c.id, c.owner, c.grant_id, c.file_id, c.title, c.location, c.text, c.index, c.doc_hash, c.modified_time, _pack(v)) for c, v in zip(chunks, vectors, strict=True)]
        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany("INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._db.execute("COMMIT")

    def remove_file(self, owner: str, file_id: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM chunks WHERE owner = ? AND file_id = ?", (owner, file_id)).rowcount

    def remove_grant(self, grant_id: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM chunks WHERE grant_id = ?", (grant_id,)).rowcount

    def _rows(self, scope: AccessScope, extra: str = "", params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        grants = sorted(scope.grant_ids)
        if not grants:
            return []
        marks = ",".join("?" for _ in grants)
        sql = f"SELECT * FROM chunks WHERE owner = ? AND grant_id IN ({marks}){extra}"  # noqa: S608 - only placeholders are formatted in
        with self._lock:
            rows = self._db.execute(sql, (scope.owner, *grants, *params)).fetchall()
        return [row for row in rows if row[1] == scope.owner and row[2] in scope.grant_ids]  # the SQL filter, checked once more

    @staticmethod
    def _chunk(row: tuple[Any, ...]) -> Chunk:
        return Chunk(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9])

    def search(self, vector: tuple[float, ...], scope: AccessScope, *, k: int, min_score: float) -> list[tuple[Chunk, float]]:
        if len(vector) != self.dimensions:
            raise VectorIndexError("the query vector has the wrong dimension")
        query = _normalised(vector)
        scored = [(self._chunk(row), sum(a * b for a, b in zip(query, _unpack(row[10]), strict=True))) for row in self._rows(scope)]
        return sorted((pair for pair in scored if pair[1] >= min_score), key=lambda pair: (-pair[1], pair[0].file_id, pair[0].index))[:k]

    def get(self, chunk_id: str, scope: AccessScope) -> Chunk | None:
        rows = self._rows(scope, " AND id = ?", (chunk_id,))
        return self._chunk(rows[0]) if rows else None

    def chunks(self, scope: AccessScope) -> list[Chunk]:
        return sorted((self._chunk(r) for r in self._rows(scope)), key=lambda c: (c.file_id, c.index))

    def close(self) -> None:
        with self._lock:
            self._db.close()
