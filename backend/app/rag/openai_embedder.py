"""Real semantic embeddings from OpenAI's embeddings API (`POST /v1/embeddings`). **Paid; tested only with an in-process
transport.** Used only when RAG_EMBEDDER=openai.

- The key goes only to api.openai.com, through the `backend` egress entry, in an Authorization header; it is never logged.
- Before each request the batch's estimated tokens are charged against `max_usd` at `price_per_mtok`. A batch that could
  pass the limit is not sent, and nothing is retried: a failed call may already have been billed.
- `dimensions` asks the model for shorter vectors; every vector that comes back must have exactly that length.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.rag.embeddings import EmbeddingUnavailable, estimated_tokens

URL = "https://api.openai.com/v1/embeddings"
BATCH = 64


class OpenAIEmbedder:
    simulated = False

    def __init__(self, egress: Any, api_key: str, *, model: str, dimensions: int, price_per_mtok: float | None, max_usd: float,
                 client: httpx.Client | None = None, timeout: float = 60.0) -> None:  # fmt: skip
        if not api_key:
            raise EmbeddingUnavailable("OPENAI_API_KEY is not configured")
        if price_per_mtok is None or price_per_mtok <= 0 or max_usd <= 0:
            raise EmbeddingUnavailable("embedding needs OPENAI_EMBEDDING_PRICE_PER_MTOK and RAG_EMBEDDING_MAX_USD, so its spending can be bounded")
        self.name = f"openai:{model}"
        self.dimensions = dimensions
        self._egress = egress
        self._key = api_key
        self._model = model
        self._price = self.price_per_mtok = price_per_mtok
        self._max_usd = self.max_usd = max_usd
        self.spent_usd = 0.0  # what was charged: the reported usage, or the worst case when none was reported
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), BATCH):
            vectors.extend(self._batch(list(texts[start : start + BATCH])))
        return vectors

    def _batch(self, batch: list[str]) -> list[tuple[float, ...]]:
        worst = estimated_tokens(batch) * self._price / 1_000_000
        if self.spent_usd + worst > self._max_usd:
            raise EmbeddingUnavailable(f"embedding stopped: the next batch could pass the ${self._max_usd:.2f} embedding limit (${self.spent_usd:.4f} used)")
        if not self._egress.check("backend", URL).allowed:
            raise EmbeddingUnavailable("the egress allowlist does not allow api.openai.com for the backend")
        body = {"model": self._model, "input": batch, "dimensions": self.dimensions, "encoding_format": "float"}
        try:
            response = self._client.post(URL, json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            self.spent_usd += worst  # it may have been billed
            raise EmbeddingUnavailable(f"OpenAI's embeddings API could not be reached: {type(exc).__name__}") from None
        if response.status_code != 200:
            self.spent_usd += worst if response.status_code >= 500 else 0.0
            raise EmbeddingUnavailable(f"OpenAI's embeddings API answered HTTP {response.status_code}")
        data = response.json()
        used = (data.get("usage") or {}).get("total_tokens")
        self.spent_usd += used * self._price / 1_000_000 if isinstance(used, int) else worst
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        vectors = [tuple(float(x) for x in row.get("embedding", [])) for row in rows]
        if len(vectors) != len(batch) or any(len(v) != self.dimensions for v in vectors):
            raise EmbeddingUnavailable("OpenAI's embeddings answer did not hold one vector of the asked length per text")
        return vectors
