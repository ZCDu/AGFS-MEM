"""
Share-link tests.

Two layers:
  - ShareLinkStore: create/get/revoke/expiry, plain CRUD over a small JSON
    record.
  - scope.py: the actual security boundary -- component_of() (what's in
    scope right now) and restrict_plan_to_scope() (does a proposed plan stay
    inside it). These are the tests that matter most: a bug here means a
    guest holding one link could write into an unrelated part of the
    owner's graph, which is exactly the guarantee this feature exists for.
"""

from __future__ import annotations

import tempfile
import time

import pytest

from app.extract.extractor import ExtractionPlan, Operation
from app.graph.store import EntityGraphStore
from app.shares.scope import component_of, restrict_plan_to_scope
from app.shares.store import ShareLinkStore
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend)
    backend.close()


@pytest.fixture()
def links(store):
    return ShareLinkStore(store.backend)


# ---------- ShareLinkStore ----------

def test_create_and_get_roundtrips(links):
    link = links.create("miguel", "event/q3-planning", label="Q3 Planning")
    fetched = links.get(link.share_id)
    assert fetched is not None
    assert fetched.owner_user_id == "miguel"
    assert fetched.entry_wiki_id == "event/q3-planning"
    assert fetched.label == "Q3 Planning"
    assert fetched.active is True


def test_unknown_share_id_returns_none(links):
    assert links.get("nonexistent-token-value-1234567") is None


def test_malformed_share_id_returns_none_not_error(links):
    assert links.get("../../etc/passwd") is None
    assert links.get("") is None


def test_revoke_deactivates(links):
    link = links.create("miguel", "event/q3-planning")
    assert links.revoke(link.share_id) is True
    assert links.get(link.share_id).active is False


def test_revoke_unknown_id_returns_false(links):
    assert links.revoke("nonexistent-token-value-1234567") is False


def test_expiry_deactivates_a_past_link(links):
    link = links.create("miguel", "event/q3-planning", expires_in_days=0.0000001)
    time.sleep(0.05)
    assert links.get(link.share_id).active is False


def test_no_expiry_stays_active(links):
    link = links.create("miguel", "event/q3-planning")
    assert links.get(link.share_id).expires_at is None
    assert links.get(link.share_id).active is True


def test_list_for_owner_filters_by_owner(links):
    links.create("miguel", "event/a")
    links.create("miguel", "event/b")
    links.create("alexei", "event/c")
    mine = links.list_for_owner("miguel")
    assert len(mine) == 2
    assert {l.entry_wiki_id for l in mine} == {"event/a", "event/b"}


# ---------- component_of ----------

def test_component_of_isolated_entity_is_itself(store):
    store.upsert_entity("u", "event", "Q3 Planning")
    store.flush()
    assert component_of(store, "u", "event/q3-planning") == {"event/q3-planning"}


def test_component_of_includes_linked_entities_only(store):
    store.upsert_entity("u", "event", "Q3 Planning")
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "Bob Nguyen")  # unrelated
    store.link_entities("u", "event/q3-planning", "person/alice-chen")
    store.flush()

    scope = component_of(store, "u", "event/q3-planning")
    assert scope == {"event/q3-planning", "person/alice-chen"}
    assert "person/bob-nguyen" not in scope


def test_component_of_missing_entity_is_empty(store):
    assert component_of(store, "u", "event/does-not-exist") == set()


def test_component_of_deleted_entity_is_empty(store):
    store.upsert_entity("u", "event", "Q3 Planning")
    store.flush()
    store.delete_entity("u", "event/q3-planning", hard_delete=True)
    assert component_of(store, "u", "event/q3-planning") == set()


# ---------- restrict_plan_to_scope ----------

def _op(op, wiki_id, payload=None, reason="") -> Operation:
    return Operation(op=op, wiki_id=wiki_id, payload=payload or {}, reason=reason)


def _plan(*ops: Operation) -> ExtractionPlan:
    return ExtractionPlan(decision="store", operations=list(ops))


def test_op_on_an_in_scope_existing_entity_is_kept(store):
    plan = _plan(_op("add_fact", "event/q3-planning", {"text": "x"}))
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert rejected == []
    assert plan.operations[0].status == "proposed"


def test_op_on_an_existing_but_unrelated_entity_is_rejected(store):
    """The core guarantee: a guest cannot touch an existing entity elsewhere
    in the owner's graph just because they mentioned its name."""
    plan = _plan(_op("add_fact", "person/some-unrelated-vp",
                     {"text": "gave a raise"}))
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    assert plan.operations[0].status == "rejected"
    assert "outside the shared scope" in plan.operations[0].detail


def test_new_entity_linked_to_scope_is_allowed_in(store):
    """A brand new person the guest introduces, linked to the shared
    meeting, becomes part of what's shared -- that's the whole point."""
    plan = _plan(
        _op("upsert_entity", "person/dana-lee",
            {"type": "person", "title": "Dana Lee"}, reason="create new entity"),
        _op("link_entities", "event/q3-planning",
            {"target_wiki_id": "person/dana-lee"}),
    )
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert rejected == []
    assert all(o.status == "proposed" for o in plan.operations)


def test_new_entity_chain_transitively_joins_scope(store):
    """B links to A (already in scope), C links to B -- C should join too,
    even though nothing links C directly to the shared entry point."""
    plan = _plan(
        _op("upsert_entity", "person/dana-lee",
            {"type": "person", "title": "Dana Lee"}, reason="create new entity"),
        _op("upsert_entity", "org/acme",
            {"type": "organization", "title": "Acme"}, reason="create new entity"),
        _op("link_entities", "event/q3-planning",
            {"target_wiki_id": "person/dana-lee"}),
        _op("link_entities", "person/dana-lee", {"target_wiki_id": "org/acme"}),
    )
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert rejected == []


def test_new_unlinked_entity_is_rejected(store):
    """A new entity the guest mentions but never connects to the shared
    topic must not be silently written -- there's no basis to say it
    belongs to what was shared."""
    plan = _plan(
        _op("upsert_entity", "person/random-friend",
            {"type": "person", "title": "Random Friend"}, reason="create new entity"),
    )
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    assert plan.operations[0].status == "rejected"


def test_cannot_bridge_to_an_existing_unrelated_entity_via_a_new_one(store):
    """The escalation this whole design exists to prevent, in its sneakiest
    form: a guest introduces a new, in-scope-linked entity (Dana Lee, tied
    to the shared meeting) and ALSO links that brand-new entity onward to
    some existing, unrelated entity elsewhere in the owner's graph. Dana Lee
    joining scope must not let the unrelated existing entity ride in behind
    her -- only NEW entities propagate into scope, never existing ones."""
    plan = _plan(
        _op("upsert_entity", "person/dana-lee",
            {"type": "person", "title": "Dana Lee"}, reason="create new entity"),
        _op("link_entities", "event/q3-planning",
            {"target_wiki_id": "person/dana-lee"}),
        _op("link_entities", "person/dana-lee",
            {"target_wiki_id": "person/some-unrelated-vp"}),
    )
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    # Dana Lee herself, and the link that brings her in, are fine.
    entity_op = next(o for o in plan.operations if o.op == "upsert_entity")
    assert entity_op.status == "proposed"
    bridge_link = next(o for o in plan.operations
                       if o.op == "link_entities"
                       and o.payload.get("target_wiki_id") == "person/dana-lee")
    assert bridge_link.status == "proposed"
    # But the second hop, reaching for the unrelated existing entity, is not.
    escalation_link = next(o for o in plan.operations
                           if o.op == "link_entities"
                           and o.payload.get("target_wiki_id") == "person/some-unrelated-vp")
    assert escalation_link.status == "rejected"
    assert len(rejected) == 1


def test_link_entities_requires_both_endpoints_in_scope(store):
    plan = _plan(_op("link_entities", "event/q3-planning",
                     {"target_wiki_id": "person/some-unrelated-vp"}))
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    assert plan.operations[0].status == "rejected"


def test_delete_entity_always_rejected_even_in_scope(store):
    plan = _plan(_op("delete_entity", "event/q3-planning",
                     {"entity": "event/q3-planning"}))
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    assert plan.operations[0].status == "rejected"
    assert "not permitted" in plan.operations[0].detail


def test_delete_fact_always_rejected_even_in_scope(store):
    plan = _plan(_op("delete_fact", "event/q3-planning",
                     {"entity": "event/q3-planning", "fact_id": "fact_0001"}))
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    assert plan.operations[0].status == "rejected"


def test_mixed_plan_keeps_in_scope_and_rejects_out_of_scope_independently(store):
    plan = _plan(
        _op("add_fact", "event/q3-planning", {"text": "moved to Friday"}),
        _op("add_fact", "person/some-unrelated-vp", {"text": "unrelated"}),
    )
    rejected = restrict_plan_to_scope(plan, {"event/q3-planning"})
    assert len(rejected) == 1
    statuses = {o.wiki_id: o.status for o in plan.operations}
    assert statuses["event/q3-planning"] == "proposed"
    assert statuses["person/some-unrelated-vp"] == "rejected"


def test_empty_scope_rejects_everything(store):
    plan = _plan(_op("add_fact", "event/q3-planning", {"text": "x"}))
    rejected = restrict_plan_to_scope(plan, set())
    assert len(rejected) == 1
