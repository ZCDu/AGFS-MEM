"""
Ranking tests for app/graph/search.py.

Runs against fake ManifestEntry objects and a stub embeddings module, no
HTTP layer and no real storage backend -- this exercises rank_entries()
directly, at the level the behavior actually lives at, and avoids the
disk-backend async-teardown flakiness that the HTTP-level tests in
test_api.py are prone to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.graph.manifest import ManifestEntry
from app.graph.search import rank_entries


def entry(wiki_id, title, compact="", aliases=None, last_accessed=""):
    return ManifestEntry(wiki_id=wiki_id, type=wiki_id.split("/")[0], title=title,
                         aliases=aliases or [], compact=compact,
                         last_accessed=last_accessed)


class FakeEmbeddingIndex:
    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def get_all(self, scope):
        return dict(self._vectors)


class FakeStore:
    def __init__(self, vectors: dict[str, list[float]]):
        self.embedding_index = FakeEmbeddingIndex(vectors)


@pytest.fixture
def stub_embeddings(monkeypatch):
    """Enable the semantic tier and stub embed() to a caller-controlled
    lookup, so a test can assign each query/entity text its own vector
    without loading the real ~1-2GB model."""
    import app.graph.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module, "is_embedding_enabled", lambda: True)

    table: dict[str, list[float]] = {}

    def fake_embed(text):
        return table.get(text)

    monkeypatch.setattr(embeddings_module, "embed", fake_embed)

    import app.config as config_module
    settings = config_module.get_settings()
    monkeypatch.setattr(type(settings), "embedding_similarity_floor",
                        property(lambda self: 0.5), raising=False)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    return table


def test_substring_hit_still_includes_description_only_entries_below_it(stub_embeddings):
    """Regression guard: a title/id substring hit must rank first, but an
    entity that only matches via description-overlap must still appear
    below it -- not be dropped once any substring hit exists elsewhere."""
    hit = entry("project/timeline-overhaul", "Timeline Overhaul")
    desc_only = entry("project/orion", "Orion",
                      compact="Includes a full timeline for the propulsion team.")
    entries = [hit, desc_only]

    ranked = rank_entries(entries, "timeline")

    assert [e.wiki_id for e in ranked] == ["project/timeline-overhaul", "project/orion"]


def test_substring_hit_skips_the_semantic_tier(stub_embeddings):
    """Cost control: embed() must not even be consulted once a substring hit
    exists anywhere in the entry set."""
    stub_embeddings["alice"] = [1.0, 0.0]  # would match if embed() ran
    hit = entry("person/alice-chen", "Alice Chen")
    store = FakeStore({"person/alice-chen": [1.0, 0.0]})

    ranked = rank_entries([hit], "alice", store=store, scope="u")

    assert [e.wiki_id for e in ranked] == ["person/alice-chen"]


def test_weak_description_hit_no_longer_hides_a_semantic_match_elsewhere(stub_embeddings):
    """The actual behavior change: previously, ANY nonzero description-tier
    hit ANYWHERE in the entry set blocked the semantic tier from running at
    all (the whole `scored` list had to be empty first) -- so a
    conceptually-related entity with zero shared words could never surface
    if some unrelated entity happened to have a weak lexical hit. Now the
    two tiers are fused instead of one gating the other, so the semantic
    match is no longer completely excluded from the results."""
    weak_lexical = entry("project/weekly-notes", "Weekly Notes",
                         compact="a general overview of last week's team status meeting")
    semantic_only = entry("organization/branch-office", "Branch Office",
                          compact="regional branch coordination and staffing")

    stub_embeddings["departmental structure overview"] = [1.0, 0.0, 0.0]
    store = FakeStore({
        "organization/branch-office": [1.0, 0.0, 0.0],   # identical to the query -> similarity 1.0
        "project/weekly-notes": [0.0, 1.0, 0.0],          # orthogonal -> similarity 0.0, below floor
    })

    ranked = rank_entries([weak_lexical, semantic_only],
                         "departmental structure overview", store=store, scope="u")

    ids = {e.wiki_id for e in ranked}
    # Under the old either/or gating, "organization/branch-office" would
    # never have been considered at all, because "project/weekly-notes"
    # already gave the description tier a (weak) nonzero hit.
    assert ids == {"project/weekly-notes", "organization/branch-office"}


def test_tied_substring_hits_break_ties_by_recency():
    """decay_score() previously existed only as an unused API display field
    (see app/graph/store.py's decay_score()). This is its first real
    consumer: when two entries land on the exact same relevance score --
    here, both are plain substring hits, tier 1's flat 1.0 -- the more
    recently touched one should surface first, rather than falling back to
    whatever order the entries happened to arrive in."""
    now = datetime.now(timezone.utc)
    stale = entry("person/alice-chen", "Alice Chen",
                  last_accessed=(now - timedelta(days=200)).isoformat())
    fresh = entry("person/alicia", "Alicia",
                  last_accessed=(now - timedelta(hours=1)).isoformat())

    ranked = rank_entries([stale, fresh], "ali")

    assert [e.wiki_id for e in ranked] == ["person/alicia", "person/alice-chen"]


def test_recency_tiebreak_never_overrides_relevance_tier():
    """The tie-break must never promote a fresher WEAK match over a stale
    STRONG one -- decay only orders entries that already tied on score.
    A substring hit (score 1.0) must still beat a description-only hit
    (score <= 0.5) even when the description hit is far more recent."""
    now = datetime.now(timezone.utc)
    stale_strong = entry("person/alice-chen", "Alice Chen",
                         last_accessed=(now - timedelta(days=365)).isoformat())
    fresh_weak = entry("project/orion", "Orion",
                       compact="Alice Chen leads this initiative",
                       last_accessed=now.isoformat())

    ranked = rank_entries([stale_strong, fresh_weak], "alice chen")

    assert [e.wiki_id for e in ranked] == ["person/alice-chen", "project/orion"]


def test_no_match_anywhere_returns_empty(stub_embeddings):
    e = entry("project/orion", "Orion", compact="propulsion system redesign")
    store = FakeStore({})
    assert rank_entries([e], "completely unrelated query", store=store, scope="u") == []
