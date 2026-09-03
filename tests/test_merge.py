"""
merge_entities() tests.

There is no automatic entity-merge anywhere else in the codebase — upsert_entity()'s
fuzzy title matching only prevents duplicates AT WRITE TIME. merge_entities() is the
tool for folding two entities together after the fact: content copied onto the
target, other entities' relations redirected (not dropped), source tombstoned with
merged_into recorded.
"""

from __future__ import annotations

import tempfile

import pytest

from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend)
    backend.close()


def test_merge_moves_facts_and_aliases(store):
    store.upsert_entity("u", "person", "Alice Chen", aliases=["A. Chen"])
    store.add_fact("u", "person/alice-chen", "Leads the Orion project")
    store.upsert_entity("u", "person", "Alicia", aliases=["Ali"])
    store.add_fact("u", "person/alicia", "Based in Berlin")
    store.flush()

    result = store.merge_entities("u", "person/alicia", "person/alice-chen")
    assert result["merged"] is True
    assert result["facts_moved"] == 1

    target = store.get_entity("u", "person/alice-chen", touch=False)
    texts = {f.text for f in target.facts}
    assert "Leads the Orion project" in texts
    assert "Based in Berlin" in texts
    assert "Alicia" in target.aliases
    assert "Ali" in target.aliases
    assert "A. Chen" in target.aliases


def test_merge_tombstones_source_with_merged_into(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.flush()

    store.merge_entities("u", "person/alicia", "person/alice-chen")

    assert store.get_entity("u", "person/alicia", touch=False) is None
    ghost = store.get_entity("u", "person/alicia", touch=False, include_deleted=True)
    assert ghost.status == "deleted"
    assert ghost.merged_into == "person/alice-chen"
    assert "person/alicia" not in store.list_entities("u")


def test_merge_redirects_other_entities_relations(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.upsert_entity("u", "project", "Orion")
    store.link_entities("u", "project/orion", "person/alicia", category="related_to")
    store.flush()

    result = store.merge_entities("u", "person/alicia", "person/alice-chen")
    assert result["relations_redirected"] == 1

    orion = store.get_entity("u", "project/orion", touch=False)
    targets = {r.target for r in orion.relations}
    assert "person/alicia" not in targets
    assert "person/alice-chen" in targets


def test_merge_redirect_collision_merges_instead_of_duplicating(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.upsert_entity("u", "project", "Orion")
    store.link_entities("u", "project/orion", "person/alice-chen", category="related_to",
                        evidence=["already-linked"])
    store.link_entities("u", "project/orion", "person/alicia", category="related_to",
                        evidence=["also-linked"])
    store.flush()

    store.merge_entities("u", "person/alicia", "person/alice-chen")

    orion = store.get_entity("u", "project/orion", touch=False)
    matches = [r for r in orion.relations
              if r.target == "person/alice-chen" and r.category == "related_to"]
    assert len(matches) == 1
    assert set(matches[0].evidence) == {"already-linked", "also-linked"}


def test_merge_copies_sources_own_relations_onto_target(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.upsert_entity("u", "project", "Orion")
    store.link_entities("u", "person/alicia", "project/orion", category="related_to")
    store.flush()

    result = store.merge_entities("u", "person/alicia", "person/alice-chen")
    assert result["relations_moved"] == 1

    target = store.get_entity("u", "person/alice-chen", touch=False)
    assert any(r.target == "project/orion" for r in target.relations)


def test_merge_drops_targets_own_relation_to_the_source(store):
    """If the target already held a relation POINTING AT the source (the two
    duplicates had been linked to each other before anyone noticed they were
    the same entity), that relation must be dropped, not left behind as a
    self-loop pointing at the now-tombstoned source id."""
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.link_entities("u", "person/alice-chen", "person/alicia", category="related_to")
    store.flush()

    store.merge_entities("u", "person/alicia", "person/alice-chen")

    target = store.get_entity("u", "person/alice-chen", touch=False)
    assert all(r.target != "person/alicia" for r in target.relations)


def test_merge_same_entity_fails(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.flush()
    result = store.merge_entities("u", "person/alice-chen", "person/alice-chen")
    assert result["merged"] is False
    assert "same entity" in result["failed"]


def test_merge_across_types_fails(store):
    store.upsert_entity("u", "person", "Orion")
    store.upsert_entity("u", "project", "Orion Two")
    store.flush()
    result = store.merge_entities("u", "project/orion-two", "person/orion")
    assert result["merged"] is False
    assert "types must match" in result["failed"]


def test_merge_missing_source_or_target_fails(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.flush()
    r1 = store.merge_entities("u", "person/nobody", "person/alice-chen")
    assert r1["merged"] is False
    r2 = store.merge_entities("u", "person/alice-chen", "person/nobody")
    assert r2["merged"] is False


def test_merge_hard_delete_removes_source_file(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Alicia")
    store.flush()

    store.merge_entities("u", "person/alicia", "person/alice-chen", hard_delete=True)

    assert store.get_entity("u", "person/alicia", touch=False, include_deleted=True) is None
