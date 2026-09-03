"""LLM CRUD-intent tests.

The /agent/act endpoint and the chat tools_enabled path let the LLM (or any
agent) read/write/update/delete on the entity graph through explicit structured
intents. These tests pin the write/update/delete surface and its permission
gating, using a stub so no live model or network is required.
"""

from __future__ import annotations

import tempfile

import pytest

from app.graph.store import EntityGraphStore
from app.intents.executor import parse_intents, execute_intents
from app.storage.mirage_backend import MirageBackend
from app.wikis.registry import WikiRegistry, ROLE_WRITE


@pytest.fixture()
def env():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    store = EntityGraphStore(backend)
    registry = WikiRegistry(backend)
    yield registry, store
    backend.close()


def _writer_all(registry, uid):
    def _w(wiki_id):
        try:
            registry.require(wiki_id, uid, ROLE_WRITE)
            return True
        except Exception:
            return False
    return _w


def test_parse_intents_extracts_block():
    reply = "Sure, I'll remember that.\n<intents>\n[{\"op\":\"add_fact\",\"entity\":\"person/sarah-kim\",\"text\":\"Likes Tuesdays\"}]\n</intents>"
    intents = parse_intents(reply)
    assert len(intents) == 1
    assert intents[0]["op"] == "add_fact"
    assert intents[0]["entity"] == "person/sarah-kim"


def test_parse_intents_empty_when_absent():
    assert parse_intents("No memory changes needed here.") == []


def test_full_crud_cycle(env):
    registry, store = env
    registry.create("Q3 Azure Migration", "alice",
                    description="The Azure migration", topic="Q3 Azure migration planning")
    writer = _writer_all(registry, "alice")

    # CREATE entity
    intents = [{"op": "create_entity", "wiki": "q3-azure-migration",
                "type": "person", "title": "Sarah Kim",
                "summary": "Leads the Azure migration."}]
    res = execute_intents(store, "alice", intents, writer_for=writer)
    assert res[0]["status"] == "applied"
    assert store.get_entity("q3-azure-migration", "person/sarah-kim", touch=False) is not None

    # ADD fact
    res = execute_intents(store, "alice", [
        {"op": "add_fact", "wiki": "q3-azure-migration", "entity": "person/sarah-kim",
         "text": "Deadline is Sept 30.", "confidence": 0.9}], writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("q3-azure-migration", "person/sarah-kim", touch=False)
    assert any("Sept 30" in f.text for f in ent.facts)
    fid = ent.facts[0].fact_id

    # UPDATE fact
    res = execute_intents(store, "alice", [
        {"op": "update_fact", "wiki": "q3-azure-migration", "entity": "person/sarah-kim",
         "fact_id": fid, "text": "Deadline moved to Oct 5."}], writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("q3-azure-migration", "person/sarah-kim", touch=False)
    assert any("Oct 5" in f.text for f in ent.facts)

    # DELETE fact
    res = execute_intents(store, "alice", [
        {"op": "delete_fact", "wiki": "q3-azure-migration",
         "entity": "person/sarah-kim", "fact_id": fid}], writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("q3-azure-migration", "person/sarah-kim", touch=False)
    assert ent.facts == []

    # DELETE entity
    res = execute_intents(store, "alice", [
        {"op": "delete_entity", "wiki": "q3-azure-migration",
         "entity": "person/sarah-kim"}], writer_for=writer)
    assert res[0]["status"] == "applied"
    assert store.get_entity("q3-azure-migration", "person/sarah-kim", touch=False) is None


def test_relation_intents(env):
    registry, store = env
    registry.create("Sales", "alice", topic="Sales")
    writer = _writer_all(registry, "alice")
    store.upsert_entity("sales", "person", "Priya", summary_append="Sales rep.")
    store.upsert_entity("sales", "organization", "Acme", summary_append="A customer.")

    res = execute_intents(store, "alice", [
        {"op": "add_relation", "wiki": "sales", "source": "person/priya",
         "target": "organization/acme", "category": "related_to", "label": "works_with"}],
        writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("sales", "person/priya", touch=False)
    rid = ent.relations[0].relation_id
    assert any(r.label == "works_with" for r in ent.relations)

    # UPDATE relation
    res = execute_intents(store, "alice", [
        {"op": "update_relation", "wiki": "sales", "source": "person/priya",
         "relation_id": rid, "label": "account_manager"}], writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("sales", "person/priya", touch=False)
    assert any(r.label == "account_manager" for r in ent.relations)

    # DELETE relation
    res = execute_intents(store, "alice", [
        {"op": "delete_relation", "wiki": "sales", "source": "person/priya",
         "relation_id": rid}], writer_for=writer)
    assert res[0]["status"] == "applied"
    ent = store.get_entity("sales", "person/priya", touch=False)
    assert ent.relations == []


def test_write_access_is_gated(env):
    registry, store = env
    registry.create("Sales", "alice", topic="Sales")
    # 'bob' has no write access.
    writer_bob = _writer_all(registry, "bob")
    res = execute_intents(store, "bob", [
        {"op": "add_fact", "wiki": "sales", "entity": "person/x", "text": "nope"}],
        writer_for=writer_bob)
    assert res[0]["status"] == "skipped"
    assert "no write access" in res[0]["message"]


def test_invalid_intent_skipped_others_apply(env):
    registry, store = env
    registry.create("Sales", "alice", topic="Sales")
    writer = _writer_all(registry, "alice")
    store.upsert_entity("sales", "person", "Priya")
    res = execute_intents(store, "alice", [
        {"op": "bogus_op", "wiki": "sales"},
        {"op": "add_fact", "wiki": "sales", "entity": "person/priya", "text": "works hard"},
    ], writer_for=writer)
    assert res[0]["status"] in ("skipped", "failed")
    assert res[1]["status"] == "applied"


def test_parse_intents_accepts_bare_json_array():
    """The retraction completion returns a bare JSON array (no <intents>
    wrapper); parse_intents must accept it."""
    from app.intents.executor import parse_intents
    assert parse_intents('[{"op":"delete_fact","entity":"person/sarah"}]') == [
        {"op": "delete_fact", "entity": "person/sarah"}]
    assert parse_intents("no memory change") == []


# ---------------------------------------------------------------------------
# retraction/correction detection (indirect commands)
# ---------------------------------------------------------------------------

def test_retraction_detector_catches_indirect_phrasings():
    from app.intents.retract import is_retraction
    for msg in (
        "Sarah no longer leads the Azure migration.",
        "The deadline is wrong, it's actually October not September.",
        "I shouldn't have told you that about Maria.",
        "I take back what I said about the coffee machine.",
        "That fact about Acme is not true anymore.",
        "Forgot the last thing I said about the deadline.",
        "Forget about the batch ETL migration.",
        "Forget about batch etl.",
    ):
        assert is_retraction(msg), msg


def test_retraction_detector_rejects_chat_and_questions():
    from app.intents.retract import is_retraction
    for msg in (
        "Hey how are you today?",
        "What is the weather like?",
        "Remember that Priya handles the Acme account.",
        "Just chatting, nothing to change.",
    ):
        assert not is_retraction(msg), msg


# ---------------------------------------------------------------------------
# Semantica-structured retraction/correction (replaces the LLM-completion path)
# ---------------------------------------------------------------------------

def test_semantica_correction_updates_fact(env):
    registry, store = env
    registry.create("Q3", "alice", topic="Q3 migration")
    store.upsert_entity("q3", "project", "q3-azure-migration")
    store.add_fact("q3", "project/q3-azure-migration", "Deadline September 30.")
    store.flush()

    def writer(w):
        try:
            registry.require(w, "alice", ROLE_WRITE); return True
        except Exception:
            return False

    from app.intents.semantica_crud import apply_semantica_conflict
    claim = {"entity": "project/q3-azure-migration", "property": "deadline",
             "value": "October"}
    results = apply_semantica_conflict(store, "alice", [claim], writer, wiki_id="q3")
    assert results[0]["status"] == "applied", results
    ent = store.get_entity("q3", "project/q3-azure-migration", touch=False)
    assert any("October" in f.text for f in ent.facts)


def test_semantica_retraction_deletes_fact(env):
    registry, store = env
    registry.create("Q3", "alice", topic="Q3")
    store.upsert_entity("q3", "person", "Sarah Kim")
    store.add_fact("q3", "person/sarah-kim", "Leads the Azure migration.")
    store.flush()

    def writer(w):
        try:
            registry.require(w, "alice", ROLE_WRITE); return True
        except Exception:
            return False

    from app.intents.semantica_crud import apply_semantica_conflict
    claim = {"entity": "person/sarah-kim", "property": "role", "value": None}
    results = apply_semantica_conflict(store, "alice", [claim], writer, wiki_id="q3")
    assert results[0]["status"] == "applied", results
    ent = store.get_entity("q3", "person/sarah-kim", touch=False)
    assert ent.facts == []


def test_semantica_forget_deletes_the_whole_entity_node(env):
    """A pure "forget X" (generic claim, value null) must remove the ENTITY
    NODE itself — not just one of its facts. A user who says "forget about
    batch etl" expects the node to be gone from the graph."""
    registry, store = env
    registry.create("Q3", "alice", topic="Q3")
    store.upsert_entity("q3", "project", "Batch ETL")
    store.add_fact("q3", "project/batch-etl", "Part of the cloud migration.")
    store.flush()

    def writer(w):
        try:
            registry.require(w, "alice", ROLE_WRITE); return True
        except Exception:
            return False

    from app.intents.semantica_crud import apply_semantica_conflict
    # Generic property + value null = "forget this thing entirely".
    claim = {"entity": "project/batch-etl", "property": "fact", "value": None}
    results = apply_semantica_conflict(store, "alice", [claim], writer, wiki_id="q3")
    assert results[0]["status"] == "applied", results
    assert store.get_entity("q3", "project/batch-etl", touch=False) is None, \
        "the entity node must be deleted on a pure forget"


def test_semantica_forget_is_staged_until_confirmed(env):
    """When confirm_deletes=True, a pure "forget X" is NOT auto-applied — it is
    staged as `needs_confirmation` (with a plan of what would be deleted) and
    nothing is removed until the user confirms the pending deletion."""
    registry, store = env
    registry.create("Q3", "alice", topic="Q3")
    store.upsert_entity("q3", "project", "Batch ETL")
    store.add_fact("q3", "project/batch-etl", "Part of the cloud migration.")
    store.flush()

    def writer(w):
        try:
            registry.require(w, "alice", ROLE_WRITE); return True
        except Exception:
            return False

    from app.intents.semantica_crud import (apply_semantica_conflict,
                                            confirm_pending_deletion)
    claim = {"entity": "project/batch-etl", "property": "fact", "value": None}

    results = apply_semantica_conflict(store, "alice", [claim], writer,
                                       wiki_id="q3", confirm_deletes=True)
    r = results[0]
    assert r["status"] == "needs_confirmation", results
    assert r["plan"]["entity"] == "project/batch-etl"
    assert store.get_entity("q3", "project/batch-etl", touch=False) is not None, \
        "node must STILL exist before confirmation"

    out = confirm_pending_deletion(store, "alice", writer, r["pending_id"])
    assert out["status"] == "applied", out
    assert store.get_entity("q3", "project/batch-etl", touch=False) is None


def test_semantica_no_write_access_skips(env):
    registry, store = env
    registry.create("Q3", "alice", topic="Q3")
    store.upsert_entity("q3", "person", "Bob")
    store.flush()

    def writer_deny(w):
        return False

    from app.intents.semantica_crud import apply_semantica_conflict
    claim = {"entity": "person/bob", "property": "fact", "value": "x"}
    results = apply_semantica_conflict(store, "mallory", [claim], writer_deny, wiki_id="q3")
    assert results[0]["status"] == "skipped", results
