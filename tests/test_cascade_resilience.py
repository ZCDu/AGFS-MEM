"""
Tests for the resilience added to delete_entity()/cascade_orphaned_relations()/
reconcile_dangling_relations(): a failure fixing ONE entity must not abort the
whole scan, and a totally broken cascade must never turn a successful
tombstone into a failed delete_entity() call.

These go through EntityGraphStore directly (not the HTTP API) since they
need to inject failures at specific points that aren't reachable through
the API surface.
"""

from __future__ import annotations

import shutil

import pytest

from app.graph.store import EntityGraphStore
from app.storage.backend import LocalFSBackend


@pytest.fixture()
def store(tmp_path):
    backend = LocalFSBackend(root=str(tmp_path / "bucket"))
    yield EntityGraphStore(backend)
    shutil.rmtree(tmp_path / "bucket", ignore_errors=True)


def test_cascade_continues_past_one_failing_entity(store, monkeypatch):
    # Alice, Bob, and Carol all point at Orion.
    store.upsert_entity("u1", "person", "Alice")
    store.upsert_entity("u1", "person", "Bob")
    store.upsert_entity("u1", "person", "Carol")
    store.upsert_entity("u1", "project", "Orion")
    store.link_entities("u1", "person/alice", "project/orion", category="related_to")
    store.link_entities("u1", "person/bob", "project/orion", category="related_to")
    store.link_entities("u1", "person/carol", "project/orion", category="related_to")

    # Make writes to Bob specifically always fail, simulating a persistent
    # error (e.g. a write conflict that exhausts its retries) for just one
    # of the three entities that need fixing.
    real_mutate = store._mutate

    def flaky_mutate(user_id, wiki_id, mutator):
        if wiki_id == "person/bob":
            raise RuntimeError("simulated persistent write failure for Bob")
        return real_mutate(user_id, wiki_id, mutator)

    monkeypatch.setattr(store, "_mutate", flaky_mutate)

    result = store.cascade_orphaned_relations("u1", "project/orion")

    # Alice and Carol got fixed despite Bob failing — the failure didn't
    # abort the rest of the scan.
    assert set(result["fixed"]) == {"person/alice", "person/carol"}
    assert result["failed"] == ["person/bob"]

    monkeypatch.setattr(store, "_mutate", real_mutate)  # restore before reading back
    assert store.get_edges("u1", "person/alice") == []
    assert store.get_edges("u1", "person/carol") == []
    # Bob's dangling relation is still there — exactly as expected, since
    # its fix failed. This is what /wiki/_reconcile exists to catch later.
    bob_relations = store.get_edges("u1", "person/bob")
    assert len(bob_relations) == 1
    assert bob_relations[0].target == "project/orion"


def test_delete_entity_succeeds_even_if_cascade_raises(store, monkeypatch):
    store.upsert_entity("u1", "person", "Alice")
    store.upsert_entity("u1", "project", "Orion")
    store.link_entities("u1", "person/alice", "project/orion", category="related_to")

    def broken_cascade(user_id, deleted_wiki_id):
        raise RuntimeError("cascade completely broken")

    monkeypatch.setattr(store, "cascade_orphaned_relations", broken_cascade)

    # The delete itself must still succeed — the tombstone write happens
    # BEFORE cascade runs, so a broken cascade can't undo that.
    result = store.delete_entity("u1", "project/orion")
    assert result is True

    assert store.get_entity("u1", "project/orion") is None
    # Alice's now-dangling relation is left for /wiki/_reconcile to catch —
    # but critically, the delete call itself didn't raise or fail.
    assert len(store.get_edges("u1", "person/alice")) == 1


def test_reconcile_continues_past_one_failing_entity(store, monkeypatch):
    store.upsert_entity("u1", "person", "Alice")
    store.upsert_entity("u1", "person", "Bob")
    store.upsert_entity("u1", "project", "Orion")
    store.link_entities("u1", "person/alice", "project/orion", category="related_to")
    store.link_entities("u1", "person/bob", "project/orion", category="related_to")

    # Delete Orion without cascading, leaving both Alice and Bob dangling.
    store.delete_entity("u1", "project/orion", cascade=False)

    real_mutate = store._mutate

    def flaky_mutate(user_id, wiki_id, mutator):
        if wiki_id == "person/bob":
            raise RuntimeError("simulated persistent write failure for Bob")
        return real_mutate(user_id, wiki_id, mutator)

    monkeypatch.setattr(store, "_mutate", flaky_mutate)

    result = store.reconcile_dangling_relations("u1")

    assert result["entities_fixed"] == 1  # only Alice
    assert result["entities_failed"] == ["person/bob"]
    assert result["relations_removed"] == 1

    monkeypatch.setattr(store, "_mutate", real_mutate)
    assert store.get_edges("u1", "person/alice") == []
    assert len(store.get_edges("u1", "person/bob")) == 1  # still dangling, as expected

    # Rerunning without the injected failure fixes what's left — this is
    # the actual "eliminate the lag" story: retry the sweep and it converges.
    result2 = store.reconcile_dangling_relations("u1")
    assert result2["entities_fixed"] == 1  # now Bob
    assert store.get_edges("u1", "person/bob") == []
