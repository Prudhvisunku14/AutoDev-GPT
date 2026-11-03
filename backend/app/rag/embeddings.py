"""Embedding providers behind one interface.

`HashingEmbedder` is a **TEST DOUBLE**: deterministic feature hashing of a text's words. Texts that share words come out
close and texts that only mean the same thing do not, so it is lexical matching, not semantic retrieval, and its
results must never be presented as production quality. It needs no model, no download and no network.

No real provider is implemented. The paid OpenAI embeddings API is not called, and no local model is downloaded; both
are listed as planned in the selector (app/rag/selection.py), where they can never be chosen.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has have how in is it its of on or that the this to was what when which who why will with does do should could would must can may".split())


class EmbeddingUnavailable(Exception):
    """No embedding provider can run. Nothing is indexed, and retrieval is not configured."""


class Embedder(Protocol):
    name: str
    dimensions: int
    simulated: bool  # True: a test double, never production retrieval

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


class HashingEmbedder:
    """TEST DOUBLE: lexical feature hashing into `dimensions` signed buckets, L2-normalised."""

    name = "hashing-test-double"
    simulated = True

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        for word, count in Counter(words(text)).items():
            digest = hashlib.sha256(word.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[bucket] += (1.0 + math.log(count)) * (1.0 if digest[4] & 1 else -1.0)
        norm = math.sqrt(sum(v * v for v in vector))
        return tuple(v / norm for v in vector) if norm else tuple(vector)


def estimated_tokens(texts: Sequence[str]) -> int:
    """A rough count (four characters a token), to keep embedding within its budget before anything is sent."""
    return sum(len(t) // 4 + 1 for t in texts)
