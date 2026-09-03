"""Aggregate-read filtering tests.

An OVERVIEW question ("what's open across all projects?", "summary of
everything") reads context from EVERY wiki the user can reach. That used to
pull in personal-profile / onboarding wikis unconditionally, so "Miguel" /
"Miguel Fu" records showed up as "memory used" on questions entirely
unrelated to them. The aggregate path now:

  (1) skips personal-profile / onboarding wikis, and
  (2) topically filters entities against the question.

This pins both behaviours without a live model or network.
"""

from __future__ import annotations

import tempfile

import pytest

from app.api.routes_chat import _is_identity_wiki, _aggregate_memory_context
from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend
from app.wikis.registry import WikiRegistry, ROLE_READ


@pytest.fixture()
def env():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    store = EntityGraphStore(backend)
    registry = WikiRegistry(backend)
    yield registry, store
    backend.close()


def _seed(registry, store):
    # A personal-profile wiki (should be excluded from aggregates).
    p = registry.create("Miguel User Profile", "alice", topic="User introduction")
    store.upsert_entity(p.wiki_id, "person", "Miguel", summary_append="The user.")
    # An onboarding wiki (also should be excluded).
    o = registry.create("Dana Onboarding", "alice", topic="New hire onboarding")
    store.upsert_entity(o.wiki_id, "person", "Miguel Fu", summary_append="A hire.")
    # A genuine project wiki with real facts.
    w = registry.create("Q3 Azure Migration", "alice", topic="Q3 Azure migration planning")
    store.upsert_entity(w.wiki_id, "project", "q3-azure-migration",
                        summary_append="Moving the customer portal to Azure in Q3.")
    store.upsert_entity(w.wiki_id, "person", "Sarah Kim",
                        summary_append="Leads the Azure migration.")
    # Grant alice read on all (creator is already admin).
    store.flush()
    registry.refresh_stats(p.wiki_id, 1, ["Miguel"])
    registry.refresh_stats(o.wiki_id, 1, ["Miguel Fu"])
    registry.refresh_stats(w.wiki_id, 2, ["q3-azure-migration", "Sarah Kim"])


def test_identity_wiki_detection(env):
    registry, store = env
    p = registry.create("Miguel User Profile", "alice", topic="User introduction")
    assert _is_identity_wiki(p)
    w = registry.create("Q3 Azure Migration", "alice", topic="Q3 Azure migration planning")
    assert not _is_identity_wiki(w)


def test_aggregate_excludes_identity_wikis(env):
    registry, store = env
    _seed(registry, store)
    ctx, used = _aggregate_memory_context(store, "alice", registry, question="what is the summary of all projects")
    titles = {u["title"] for u in used}
    assert "Miguel" not in titles, titles
    assert "Miguel Fu" not in titles, titles


def test_aggregate_topical_filters_entities(env):
    registry, store = env
    _seed(registry, store)
    # A question about Azure should surface relevant entities only, and never
    # the identity records. Relevance is judged by the assessor (Sarah Kim
    # matches via her summary mentioning Azure), not by literal title text.
    ctx, used = _aggregate_memory_context(store, "alice", registry,
                                          question="what deadlines are open in the azure migration")
    titles = {u["title"] for u in used}
    assert "Miguel" not in titles and "Miguel Fu" not in titles, titles
    assert titles, "the question should surface the Azure-relevant entities"
    assert any("azure" in (t + " " + (ctx or "")).lower() for t in titles)\
        or "azure" in ctx.lower(), (ctx, titles)


def test_aggregate_without_question_keeps_substance_but_no_identity(env):
    registry, store = env
    _seed(registry, store)
    ctx, used = _aggregate_memory_context(store, "alice", registry, question="")
    titles = {u["title"] for u in used}
    assert "Miguel" not in titles and "Miguel Fu" not in titles, titles
    assert titles, "with no question we still surface substantive entities"
