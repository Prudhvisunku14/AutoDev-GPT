"""The knowledge base, offline: the embedding test double, scoped search, updates and deletions, revocation, citations
that trace back, and the choice of embedding provider. The embedder is a hashing TEST DOUBLE (lexical, not semantic)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.drive import fixtures
from app.drive.model import TXT, DriveFile
from app.rag.embeddings import EmbeddingUnavailable, HashingEmbedder
from app.rag.extract import extract
from app.rag.index import AccessScope, InMemoryVectorIndex, VectorIndexError
from app.rag.knowledge import NOT_FOUND, Answer, BudgetExceeded, Citation, KnowledgeBase
from app.rag.selection import EMBEDDING, build_embedding_selector, chosen_embedder
from app.selection.model import ToolStatus
from app.selection.selector import NoProvider

G1, G2 = "grant-0000000000000001", "grant-0000000000000002"
SCOPE = AccessScope("p1", frozenset({G1}))
CHUNKING = b"Chunking guidelines\n\nSplit documents into chunks of roughly 800 characters with an overlap of about 15 percent.\n\nKeep the title and location with every chunk."
CITATIONS = b"Citation practices\n\nA citation names the source document and the location inside it."


def doc(content: bytes, file_id: str = "fxTxtChunking001", grant: str = G1, title: str = "Chunking guidelines.txt"):  # noqa: ANN201
    return extract(DriveFile(file_id, title, TXT, (fixtures.RESEARCH,), "2026-10-01T09:00:00Z", None), content, grant_id=grant)


@pytest.fixture
def base() -> KnowledgeBase:
    kb = KnowledgeBase(HashingEmbedder(), chunk_chars=400, chunk_overlap=40)
    kb.add(doc(CHUNKING), owner="p1")
    kb.add(doc(CITATIONS, "fxDocCitation001", title="Citation practices"), owner="p1")
    return kb


def test_the_hashing_embedder_is_a_deterministic_lexical_test_double() -> None:
    embedder = HashingEmbedder()
    a, b = embedder.embed(["chunks overlap characters", "chunks overlap characters"])
    assert embedder.simulated and a == b and abs(sum(v * v for v in a) - 1.0) < 1e-9
    car, automobile = embedder.embed(["car", "automobile"])
    assert abs(sum(x * y for x, y in zip(car, automobile, strict=True))) < 0.2  # same meaning, no shared word: not similar. Lexical only


def test_search_never_scores_a_chunk_outside_the_scope(base: KnowledgeBase) -> None:
    base.add(doc(CHUNKING, "fxOtherOwner0001"), owner="p2")  # the very same text, another owner
    base.add(doc(CHUNKING, "fxOtherGrant0001", grant=G2), owner="p1")  # same owner, an authorization not in the scope
    found = base.retrieve("chunks of roughly 800 characters", SCOPE, k=10, min_score=0.0)
    assert found and {c.file_id for c, _ in found} <= {"fxTxtChunking001", "fxDocCitation001"} and all(c.owner == "p1" and c.grant_id == G1 for c, _ in found)
    assert base.retrieve("chunks", AccessScope("p3", frozenset({G1})), k=10, min_score=0.0) == []
    assert base.retrieve("chunks", AccessScope("p1", frozenset()), k=10, min_score=0.0) == []


def test_the_index_refuses_vectors_of_the_wrong_dimension() -> None:
    index = InMemoryVectorIndex("hashing-test-double", 4)
    with pytest.raises(VectorIndexError):
        index.search((1.0, 0.0), SCOPE, k=1, min_score=0.0)


def test_documents_are_added_once_replaced_when_changed_and_removed_when_gone(base: KnowledgeBase) -> None:
    assert base.add(doc(CHUNKING), owner="p1").status == "unchanged"
    changed = base.add(doc(CHUNKING.replace(b"800", b"500")), owner="p1")
    assert changed.status == "updated" and all("800" not in c.text for c in base.index.chunks(SCOPE))
    assert base.sync("p1", SCOPE, {"fxTxtChunking001": "2026-10-15T10:00:00Z"}) == ["fxDocCitation001"]
    assert set(base.documents(SCOPE)) == {"fxTxtChunking001"}


def test_revocation_removes_every_chunk_of_that_grant_and_no_other(base: KnowledgeBase) -> None:
    base.add(doc(CITATIONS, "fxSecondGrant001", grant=G2), owner="p1")
    removed = base.revoke(G1)
    both = AccessScope("p1", frozenset({G1, G2}))
    assert removed > 0 and {c.file_id for c in base.index.chunks(both)} == {"fxSecondGrant001"}
    assert base.add(doc(CHUNKING), owner="p1").status == "added"  # re-read under a new grant, it is indexed afresh


def test_an_answer_cites_retrieved_chunks_that_hold_its_quotes(base: KnowledgeBase) -> None:
    answer = base.answer("How many characters should chunks have, and what overlap?", SCOPE)
    assert answer.grounded and answer.citations and "800" in answer.text
    assert all(c.chunk_id in answer.retrieved and c.quote in base.index.get(c.chunk_id, SCOPE).text for c in answer.citations)
    assert base.check_citations(answer, SCOPE) == []


def test_a_question_the_documents_cannot_answer_is_declined_with_no_citation(base: KnowledgeBase) -> None:
    answer = base.answer("What is the population of Lisbon?", SCOPE)
    assert (answer.grounded, answer.text, answer.citations) == (False, NOT_FOUND, ())


def test_untraceable_citations_are_caught(base: KnowledgeBase) -> None:
    good = base.answer("How many characters should chunks have?", SCOPE)
    real = good.citations[0]
    for forged, problem in (
        (replace(real, chunk_id="0" * 24), "does not exist"),
        (replace(real, quote="Chunks must be exactly 42 characters."), "does not contain"),
        (replace(real, doc_hash="0" * 64), "outdated"),
    ):
        assert any(problem in p for p in base.check_citations(replace(good, citations=(forged,), retrieved=(*good.retrieved, forged.chunk_id)), SCOPE))
    unretrieved = Answer(good.question, good.text, good.citations, (), True)
    assert any("not retrieved" in p for p in base.check_citations(unretrieved, SCOPE))
    declined_but_cited = Answer("q", NOT_FOUND, (Citation(real.file_id, real.title, real.location, real.chunk_id, real.modified_time, real.doc_hash, real.quote),), (real.chunk_id,), False)
    assert any("must cite nothing" in p for p in base.check_citations(declined_but_cited, SCOPE))
    base.revoke(G1)
    assert any("does not exist" in p for p in base.check_citations(good, AccessScope("p1", frozenset())))  # nothing survives a revocation


def test_without_an_embedder_nothing_is_indexed_and_nothing_is_invented() -> None:
    kb = KnowledgeBase(None)
    with pytest.raises(EmbeddingUnavailable):
        kb.add(doc(CHUNKING), owner="p1")
    with pytest.raises(EmbeddingUnavailable):
        kb.answer("anything", SCOPE)


def test_the_embedding_budget_is_checked_before_anything_is_embedded() -> None:
    kb = KnowledgeBase(HashingEmbedder(), max_embedding_tokens=5)
    with pytest.raises(BudgetExceeded, match="over the 5 limit"):
        kb.add(doc(CHUNKING), owner="p1")
    assert kb.index.chunks(SCOPE) == []


# --- Choosing the embedding provider --------------------------------------------------------------------------------


def test_the_selector_chooses_the_test_double_only_because_nothing_real_is_eligible() -> None:
    selection = build_embedding_selector([HashingEmbedder()]).choose(EMBEDDING)
    assert chosen_embedder(selection).name == "hashing-test-double" and selection.simulated
    rejected = {r["tool"]: r["reasons"] for r in selection.details()["rejected"]}
    assert set(rejected) == {"local_embedding_model"} and all("planned" in " ".join(r) for r in rejected.values())  # OpenAI's is implemented (RAG_EMBEDDER=openai)


def test_a_real_provider_always_outranks_the_test_double() -> None:
    class Real(HashingEmbedder):
        name = "hashing-test-double"
        simulated = False

    selection = build_embedding_selector([Real()]).choose(EMBEDDING)
    assert not selection.simulated


def test_with_no_provider_the_selector_names_every_reason() -> None:
    with pytest.raises(NoProvider) as raised:
        build_embedding_selector([]).choose(EMBEDDING)
    assert set(raised.value.excluded) == {"local_embedding_model"}
    from app.rag.selection import PLANNED_EMBEDDERS

    assert all(p.status is ToolStatus.PLANNED for p in PLANNED_EMBEDDERS)
