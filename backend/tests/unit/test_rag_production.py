"""Phase 6: real embeddings through OpenAI's API (faked here, in process) and a persistent, project-isolated vector index.

The fake endpoint returns *concept* vectors (synonyms share a dimension), so these tests show the pipeline retrieves by
meaning when its embedder does. They say nothing about any real model's retrieval quality: only a live, approved run does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.egress import EgressPolicy
from app.rag.chunking import Chunk
from app.rag.embeddings import EmbeddingUnavailable, HashingEmbedder
from app.rag.extract import ExtractedDocument, Section
from app.rag.index import AccessScope, VectorIndexError
from app.rag.knowledge import KnowledgeBase
from app.rag.openai_embedder import OpenAIEmbedder
from app.rag.selection import EMBEDDING, build_embedding_selector, chosen_embedder
from app.rag.sqlite_index import SqliteVectorIndex
from app.selection.model import SelectionContext
from app.tools.adapters.drive import build_drive_runtime, real_embedding
from tests.helpers import ALLOWLIST_FILE, FAKE_OPENAI_KEY, make_settings

EGRESS = EgressPolicy.from_file(ALLOWLIST_FILE)
CONCEPTS = [{"car", "automobile", "vehicle", "cars"}, {"refund", "reimbursement", "repayment"}, {"holiday", "vacation", "leave"}, {"invoice", "bill"}]
DIMS = 8


def concept_vector(text: str) -> list[float]:
    vector = [0.0] * DIMS
    for word in text.lower().replace(".", " ").replace("?", " ").split():
        for n, concept in enumerate(CONCEPTS):
            if word in concept:
                vector[n] += 1.0
    vector[DIMS - 1] = 0.05  # never all zero
    return vector


class FakeOpenAI:
    def __init__(self, status: int = 200, wrong_length: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.wrong_length = wrong_length

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "no"}})
        body = json.loads(request.content)
        data = [{"index": i, "embedding": concept_vector(t)[: DIMS - (1 if self.wrong_length else 0)]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data)), "usage": {"total_tokens": sum(len(t) // 4 + 1 for t in body["input"])}})  # out of order on purpose

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def embedder(fake: FakeOpenAI, *, max_usd: float = 1.0, price: float | None = 0.02) -> OpenAIEmbedder:
    return OpenAIEmbedder(EGRESS, FAKE_OPENAI_KEY, model="text-embedding-3-small", dimensions=DIMS, price_per_mtok=price, max_usd=max_usd, client=fake.client())


def document(file_id: str, text: str, *, grant: str = "grant-a", content_hash: str = "h1") -> ExtractedDocument:
    return ExtractedDocument(file_id, f"Doc {file_id}", "text", "2026-11-01T00:00:00Z", grant, content_hash, (Section("paragraph 1", text),), ())


# --- The embedder --------------------------------------------------------------------------------------------------


def test_the_request_is_bounded_and_the_key_goes_only_to_openai() -> None:
    fake = FakeOpenAI()
    vectors = embedder(fake).embed(["A car for sale.", "Ask for a refund."])
    [request] = fake.requests
    assert request.url == "https://api.openai.com/v1/embeddings" and request.headers["authorization"] == f"Bearer {FAKE_OPENAI_KEY}"
    assert json.loads(request.content) == {"model": "text-embedding-3-small", "input": ["A car for sale.", "Ask for a refund."], "dimensions": DIMS, "encoding_format": "float"}
    assert vectors[0][0] == 1.0 and vectors[1][1] == 1.0  # reordered by index, as OpenAI numbers them


def test_a_batch_that_could_pass_the_limit_is_never_sent() -> None:
    fake = FakeOpenAI()
    with pytest.raises(EmbeddingUnavailable, match="could pass the \\$0.00 embedding limit|could pass the"):
        embedder(fake, max_usd=0.000001).embed(["x" * 4000])
    assert fake.requests == []


def test_spending_cannot_be_unbounded() -> None:
    with pytest.raises(EmbeddingUnavailable, match="OPENAI_EMBEDDING_PRICE_PER_MTOK"):
        embedder(FakeOpenAI(), price=None)


@pytest.mark.parametrize(("fake", "message"), [(FakeOpenAI(status=401), "HTTP 401"), (FakeOpenAI(wrong_length=True), "one vector of the asked length")])
def test_a_refused_or_malformed_answer_indexes_nothing(fake: FakeOpenAI, message: str) -> None:
    with pytest.raises(EmbeddingUnavailable, match=message):
        embedder(fake).embed(["a car"])
    assert len(fake.requests) == 1  # no retry


def test_the_real_embedder_is_chosen_over_the_test_double() -> None:
    real = embedder(FakeOpenAI())
    selection = build_embedding_selector([HashingEmbedder(), real]).choose(EMBEDDING, SelectionContext(remaining_usd=1.0, remaining_tokens=1_000_000))
    assert chosen_embedder(selection) is real and not chosen_embedder(selection).simulated


# --- The persistent index ------------------------------------------------------------------------------------------


def chunk(cid: str, owner: str, grant: str, file_id: str = "f1", text: str = "t") -> Chunk:
    return Chunk(cid, owner, grant, file_id, "title", "paragraph 1", text, 0, "h1", "2026-11-01")


def test_vectors_survive_a_restart_and_reads_stay_inside_the_project_and_its_active_grants(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    index = SqliteVectorIndex(path, "openai:m", 3)
    index.upsert([chunk("a", "p1", "g1"), chunk("b", "p2", "g2"), chunk("c", "p1", "g-revoked")], [(1, 0, 0), (1, 0, 0), (1, 0, 0)])
    index.close()
    again = SqliteVectorIndex(path, "openai:m", 3)
    found = again.search((1.0, 0.0, 0.0), AccessScope("p1", frozenset({"g1"})), k=10, min_score=0.0)
    assert [c.id for c, _ in found] == ["a"]  # not p2's chunk, not the revoked grant's
    assert again.get("b", AccessScope("p1", frozenset({"g1", "g2"}))) is None  # another project's chunk, even with its grant ID
    assert again.chunks(AccessScope("p1", frozenset())) == []
    assert again.remove_grant("g-revoked") == 1


def test_an_index_built_by_another_model_is_refused(tmp_path: Path) -> None:
    SqliteVectorIndex(tmp_path / "i.sqlite3", "openai:a", 3).close()
    with pytest.raises(VectorIndexError, match="cannot be compared"):
        SqliteVectorIndex(tmp_path / "i.sqlite3", "openai:b", 3)
    with pytest.raises(VectorIndexError, match="wrong dimension"):
        SqliteVectorIndex(tmp_path / "j.sqlite3", "openai:a", 3).search((1.0, 0.0), AccessScope("p", frozenset({"g"})), k=1, min_score=0)


def test_retrieval_by_meaning_with_citations_updates_and_revocation(tmp_path: Path) -> None:
    fake = FakeOpenAI()
    real = embedder(fake)
    base = KnowledgeBase(real, index=SqliteVectorIndex(tmp_path / "kb.sqlite3", real.name, DIMS), chunk_chars=200, chunk_overlap=0)
    scope = AccessScope("p1", frozenset({"grant-a"}))
    base.add(document("cars", "Every vehicle is serviced each spring."), owner="p1")
    base.add(document("money", "A reimbursement is paid within ten days."), owner="p1")
    base.add(document("other", "Every vehicle of another project.", grant="grant-x"), owner="p2")
    found = base.retrieve("When is my automobile looked at?", scope, k=1, min_score=0.5)
    assert [c.file_id for c, _ in found] == ["cars"]  # no shared word with the question: found by meaning
    assert all(c.owner == "p1" for c, _ in base.retrieve("vehicle", scope, k=10, min_score=0.0))
    base.add(document("cars", "Each car is now serviced in autumn.", content_hash="h2"), owner="p1")  # the document changed
    [(updated, _score)] = base.retrieve("automobile", scope, k=1, min_score=0.5)
    assert "autumn" in updated.text and updated.doc_hash == "h2" and len(base.index.chunks(scope)) == 2
    base.index.remove_grant("grant-a")
    assert base.retrieve("automobile", scope, k=5, min_score=0.0) == []


def test_unsupported_input_is_refused_before_anything_is_embedded(tmp_path: Path) -> None:
    fake = FakeOpenAI()
    real = embedder(fake, max_usd=0.0000001)
    base = KnowledgeBase(real, index=SqliteVectorIndex(tmp_path / "kb.sqlite3", real.name, DIMS))
    with pytest.raises(EmbeddingUnavailable):
        base.add(document("big", "word " * 5000), owner="p1")
    assert fake.requests == [] and base.index.chunks(AccessScope("p1", frozenset({"grant-a"}))) == []


# --- Wiring --------------------------------------------------------------------------------------------------------


def test_rag_embedder_openai_wires_the_real_embedder_and_a_persistent_index(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, rag_embedder="openai", openai_embedding_price_per_mtok=0.02, rag_index_dir=tmp_path / "rag", drive_source="google",
                             google_oauth_client_id="client.apps.googleusercontent.com")  # fmt: skip
    embedders, factory = real_embedding(settings, EGRESS)
    assert [e.name for e in embedders] == ["openai:text-embedding-3-small"] and not embedders[0].simulated
    assert factory is not None and isinstance(factory(embedders[0]), SqliteVectorIndex)
    runtime = build_drive_runtime(settings, EGRESS)
    assert runtime is not None and runtime.embedders and runtime.index_factory is not None


def test_without_a_price_or_the_setting_there_is_no_real_embedder(tmp_path: Path) -> None:
    assert real_embedding(make_settings(tmp_path, rag_embedder="openai"), EGRESS) == ((), None)
    assert real_embedding(make_settings(tmp_path), EGRESS) == ((), None)


def _unused(_: Any) -> None:  # keeps the typing import used when the file is trimmed
    return None
