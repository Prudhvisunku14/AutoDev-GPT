"""Choosing the embedding provider with the same adaptive selector that chooses the coding provider.

Only providers that exist can win. The hashing test double is simulated, so any real provider would always be preferred
to it; the OpenAI embeddings API and a local model are listed as planned, so a report shows they were considered and
why they could not be used. With no provider at all, `NoProvider` names every reason, and the recovery engine stops the
run (app/tools/adapters/drive.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.embeddings import Embedder, HashingEmbedder
from app.selection.adaptive import AdaptiveSelector, EligibilityPolicy
from app.selection.catalog import IN_PROCESS, OPENAI_HOST, OPENAI_KEY_NAME
from app.selection.model import BUILD, ToolProfile, ToolStatus
from app.selection.selector import ProviderOption

EMBEDDING = "embedding"
_KINDS = {BUILD: 1.0}
PROFILES = {
    # Lexical only, hence the low declared quality: it is chosen only when nothing real is eligible.
    HashingEmbedder.name: ToolProfile(HashingEmbedder.name, ToolStatus.IMPLEMENTED, {EMBEDDING: 0.2}, {}, _KINDS, environment=IN_PROCESS, quality=0.1, seconds=0.0, cost_usd=0.0,
                                      min_cost_usd=0.0, memory_mb=16),
}  # fmt: skip
OPENAI_API = "openai-api"
PLANNED_EMBEDDERS = (ToolProfile("local_embedding_model", ToolStatus.PLANNED, {EMBEDDING: 0.8}, {}, _KINDS, environment="local-model"),)


def _profile(embedder: Embedder) -> ToolProfile:
    """A real OpenAI model (rag/openai_embedder.py) is semantic and paid; the hashing double is lexical and free."""
    if embedder.name in PROFILES:
        return PROFILES[embedder.name]
    worst = getattr(embedder, "max_usd", None)  # its own hard limit: it never spends more (rag/openai_embedder.py)
    floor = getattr(embedder, "price_per_mtok", 0.0) * 1_000 / 1_000_000  # one small batch
    return ToolProfile(embedder.name, ToolStatus.IMPLEMENTED, {EMBEDDING: 1.0}, {}, _KINDS, environment=OPENAI_API, quality=0.9, cost_usd=worst, min_cost_usd=floor,
                       credentials=frozenset({OPENAI_KEY_NAME}), network=frozenset({OPENAI_HOST}))  # fmt: skip


@dataclass(frozen=True)
class _Ready:
    """What the selector asks a provider: can it run now? An in-process embedder always can."""

    embedder: Embedder

    def unavailable_reason(self) -> str | None:
        return None


def build_embedding_selector(embedders: Sequence[Embedder]) -> AdaptiveSelector:
    options = [ProviderOption(e.name, _Ready(e), frozenset({EMBEDDING}), simulated=e.simulated, profile=_profile(e)) for e in embedders]  # type: ignore[arg-type]
    real = any(not e.simulated for e in embedders)  # a real embedder is configured only with its key and the egress entry (tools/adapters/drive.py)
    policy = EligibilityPolicy(approved_environments=frozenset({IN_PROCESS, OPENAI_API}), configured_credentials=frozenset({OPENAI_KEY_NAME}) if real else frozenset(),
                               approved_network=frozenset({OPENAI_HOST}) if real else frozenset())  # fmt: skip
    return AdaptiveSelector(options, policy=policy, planned=PLANNED_EMBEDDERS)


def chosen_embedder(selection: object) -> Embedder:
    return selection.launcher.embedder  # type: ignore[attr-defined]
