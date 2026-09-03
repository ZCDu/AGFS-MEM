"""
Scope enforcement for share-link chat.

A share grants a guest write access, via the extractor, bounded to ONE
connected component of the owner's graph (see app/graph/components.py) --
the entry entity plus everything already linked to it. This module is what
actually enforces that boundary against a proposed extraction plan, so the
guarantee is structural (an out-of-scope operation is marked rejected before
apply() ever sees it -- see ConversationExtractor.apply(), which already
skips any operation whose status is "rejected"), not a prompt instruction the
model could ignore.
"""

from __future__ import annotations

from app.extract.extractor import ExtractionPlan
from app.graph.components import build_adjacency, connected_components
from app.graph.store import EntityGraphStore

# Deletions are never applied through a share, in or out of scope -- a guest
# removing the owner's data (even data about the shared topic) is a decision
# the owner should make in their own session, not something a link a
# stranger holds can trigger unattended.
_ALWAYS_REJECTED_OPS = {"delete_entity", "delete_fact"}


def component_of(store: EntityGraphStore, owner_user_id: str,
                 entry_wiki_id: str) -> set[str]:
    """The live connected component (existing, active entities only)
    containing entry_wiki_id. Empty if entry_wiki_id no longer exists
    (deleted, merged away) -- a share whose anchor is gone grants nothing,
    rather than falling back to some other scope."""
    entries = [e for e in store.manifest.list_entries(owner_user_id)
              if e.status == "active"]
    if not any(e.wiki_id == entry_wiki_id for e in entries):
        return set()
    adj = build_adjacency(entries)
    for comp in connected_components(adj):
        if entry_wiki_id in comp:
            return set(comp)
    return {entry_wiki_id}  # isolated node: no edges yet, still a valid scope


def restrict_plan_to_scope(plan: ExtractionPlan, allowed: set[str]) -> list[dict]:
    """Mutates `plan` in place: marks every operation that would touch
    something outside `allowed` as status="rejected", so ConversationExtractor
    .apply() skips it entirely. Returns the rejected operations (as dicts)
    for the caller to report back -- "this wasn't saved, and here's why" --
    rather than silently dropping them.

    A NEW entity (this plan is creating it, not matching an existing one --
    see _build_plan()'s `matched` distinction, mirrored here via
    op.reason) is allowed into scope if the plan links it, directly or
    transitively through other new entities, to something already in
    `allowed`. An EXISTING entity is NEVER added to `allowed` this way --
    only something already genuinely connected in the live graph (or that
    was just born from this exact plan) can be in scope. Without that
    asymmetry, a guest could reach into an unrelated part of the owner's
    graph just by naming and linking to some other existing entity.
    """
    new_entity_ids = {
        op.wiki_id for op in plan.operations
        if op.op == "upsert_entity" and op.reason == "create new entity"
    }
    plan_edges = [
        (op.wiki_id, op.payload.get("target_wiki_id"))
        for op in plan.operations if op.op == "link_entities"
    ]

    scope = set(allowed)
    changed = True
    while changed:
        changed = False
        for a, b in plan_edges:
            if a in scope and b in new_entity_ids and b not in scope:
                scope.add(b)
                changed = True
            if b in scope and a in new_entity_ids and a not in scope:
                scope.add(a)
                changed = True

    rejected: list[dict] = []
    for op in plan.operations:
        if op.status == "rejected":
            continue  # already excluded for another reason upstream
        if op.op in _ALWAYS_REJECTED_OPS:
            in_scope = False
            why = "deletions are not permitted through a shared link"
        elif op.op == "link_entities":
            target = op.payload.get("target_wiki_id")
            in_scope = op.wiki_id in scope and target in scope
            why = "outside the shared scope for this link"
        else:
            in_scope = op.wiki_id in scope
            why = "outside the shared scope for this link"
        if not in_scope:
            op.status = "rejected"
            op.detail = (op.detail + "; " if op.detail else "") + why
            rejected.append(op.to_dict())
    return rejected
