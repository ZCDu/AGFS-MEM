from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Depends, HTTPException, Query

from app.api.models import (  # noqa: I001
    SaveLayoutRequest, SubgraphRequest, SubgraphResponse,  # noqa: I001
    SetPositionsRequest, SubgraphRequest,
    AddFactRequest,
    EntityOut,
    FactOut,
    GraphStatsResponse,
    InboxCandidateOut,
    LinkEntitiesRequest,
    ManifestEntryOut,
    RelationOut,
    ReconcileResponse,
    ResolveInboxCandidateRequest,
    ResolveRequest,
    ResolveResponse,
    TraverseRequest,
    TraverseResponse,
    UpdateFactRequest,
    UpdateRelationRequest,
    UpsertEntityRequest,
)
from app.auth import require_user
from app.deps import get_graph_store, get_title_resolver
from app.graph.store import (VALID_TYPES, Entity, EntityGraphStore,
                             MalformedEntityError, SlugConflictError)
from app.graph.title_resolver import WikiTitleResolver

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}", tags=["wiki"])


def _to_out(entity: Entity) -> EntityOut:
    return EntityOut(
        okf_version=entity.okf_version,
        wiki_id=entity.wiki_id,
        type=entity.type,
        title=entity.title,
        aliases=entity.aliases,
        compact=entity.compact,
        summary=entity.summary,
        facts=[FactOut(**f.to_dict()) for f in entity.facts],
        relations=[RelationOut(**r.to_dict()) for r in entity.relations],
        status=entity.status,
        merged_into=entity.merged_into,
        metadata=entity.metadata.to_dict(),
        decay_score=entity.decay_score(),
    )


def _wiki_id(type: str, title: str) -> str:
    if type not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unknown type {type!r} (expected one of {sorted(VALID_TYPES)})"
        )
    return EntityGraphStore.compute_wiki_id(type, title)


@router.put("/wiki", response_model=EntityOut)
def upsert_entity(
    user_id: str,
    body: UpsertEntityRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Create an entity if it doesn't exist, or update it if it does.
    `type` is fixed at creation — see app/graph/store.py's docstring on why
    type reclassification isn't supported."""
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unknown type {body.type!r} (expected one of {sorted(VALID_TYPES)})"
        )
    try:
        entity = store.upsert_entity(
            user_id, body.type, body.title,
            aliases=body.aliases, summary_append=body.summary_append,
            compact=body.compact, significance=body.significance,
            on_conflict=body.on_conflict,
        )
    except MalformedEntityError:
        raise
    except SlugConflictError:
        # Handled at app level, which answers 409 with both titles and a
        # usable alternative. Re-raised so the bare `except ValueError` below
        # does not flatten it into a 422 that loses that detail.
        raise
    except ValueError as e:
        # Unusable title, bad on_conflict value: the request is well-formed
        # JSON but semantically invalid.
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _to_out(entity)


@router.get("/wiki", response_model=list[ManifestEntryOut])
def list_entities(
    user_id: str,
    type: str | None = Query(None, description="Filter by entity type"),
    include_deleted: bool = Query(False, description="Include tombstoned (status=deleted) entities"),
    q: str | None = Query(None, description="Case-insensitive substring match on title, "
                                           "aliases or wiki_id"),
    limit: int | None = Query(None, ge=1, le=1000,
                              description="Page size. Omit to return everything."),
    offset: int = Query(0, ge=0, description="Rows to skip; use with limit"),
    store: EntityGraphStore = Depends(get_graph_store),
):
    """The CATALOGUE view: a flat, filterable, pageable list of entities.

    Paging is correct here because this is a list. It is NOT how to fetch the
    graph — page 2 of a graph is a set of nodes whose edges point mostly at
    nodes you were not sent, which cannot be laid out and cannot be told apart
    from genuinely dangling references. Use POST /wiki/subgraph for that; it
    returns a closed neighbourhood.

    `limit` is optional and unset by default, so existing clients are
    unaffected.
    """
    entries = store.manifest.list_entries(user_id, type_filter=type)
    if not include_deleted:
        entries = [e for e in entries if e.status != "deprecated"]
    if q:
        needle = q.strip().lower()
        entries = [e for e in entries
                   if needle in e.title.lower()
                   or needle in e.wiki_id.lower()
                   or any(needle in a.lower() for a in e.aliases)]
    entries.sort(key=lambda e: (e.type, e.title.lower()))
    if limit is not None:
        entries = entries[offset:offset + limit]
    elif offset:
        entries = entries[offset:]
    return [ManifestEntryOut(**e.to_dict()) for e in entries]


@router.post("/wiki/resolve", response_model=ResolveResponse)
def resolve_title(
    user_id: str,
    body: ResolveRequest,
    resolver: WikiTitleResolver = Depends(get_title_resolver),
):
    """Wiki Title Resolver (PLAN.md §7.3): decides whether `title` matches
    an existing entity (merges into it), is new (creates it, only if
    `type_hint` is given), or is ambiguous/unclassifiable (held in
    /wiki/_inbox for manual resolution)."""
    try:
        result = resolver.resolve(
            user_id, body.title, type_hint=body.type_hint, aliases=body.aliases,
            summary_append=body.summary_append, compact=body.compact,
            significance=body.significance,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return ResolveResponse(
        action=result.action, wiki_id=result.wiki_id, inbox_id=result.inbox_id,
        reason=result.reason, entity=_to_out(result.entity) if result.entity else None,
    )


@router.get("/wiki/_inbox", response_model=list[InboxCandidateOut])
def list_inbox(user_id: str, resolver: WikiTitleResolver = Depends(get_title_resolver)):
    return [InboxCandidateOut(**c.to_dict()) for c in resolver.inbox.list(user_id)]


@router.get("/wiki/_inbox/{candidate_id}", response_model=InboxCandidateOut)
def get_inbox_candidate(
    user_id: str, candidate_id: str,
    resolver: WikiTitleResolver = Depends(get_title_resolver),
):
    candidate = resolver.inbox.get(user_id, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Inbox candidate {candidate_id!r} not found")
    return InboxCandidateOut(**candidate.to_dict())


@router.post("/wiki/_inbox/{candidate_id}/resolve", response_model=ResolveResponse)
def resolve_inbox_candidate(
    user_id: str, candidate_id: str,
    body: ResolveInboxCandidateRequest,
    resolver: WikiTitleResolver = Depends(get_title_resolver),
):
    """Manually assign a type (and optionally a new title) to a held
    candidate. Still runs through the same matching logic, so it merges
    into an existing entity of that type if one matches, rather than
    blindly creating a duplicate."""
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unknown type {body.type!r} (expected one of {sorted(VALID_TYPES)})"
        )
    try:
        result = resolver.resolve_inbox_candidate(user_id, candidate_id, body.type, title=body.title)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return ResolveResponse(
        action=result.action, wiki_id=result.wiki_id, inbox_id=result.inbox_id,
        reason=result.reason, entity=_to_out(result.entity) if result.entity else None,
    )


@router.delete("/wiki/_inbox/{candidate_id}", status_code=204)
def discard_inbox_candidate(
    user_id: str, candidate_id: str,
    resolver: WikiTitleResolver = Depends(get_title_resolver),
):
    existed = resolver.discard_inbox_candidate(user_id, candidate_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Inbox candidate {candidate_id!r} not found")


@router.get("/wiki/{type}/{title}", response_model=EntityOut)
def get_entity(
    user_id: str,
    type: str,
    title: str,
    touch: bool = Query(True, description="Update last_accessed (retention decay clock)"),
    include_deleted: bool = Query(False, description="Include tombstoned (status=deleted) entities"),
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    entity = store.get_entity(user_id, wiki_id, touch=touch, include_deleted=include_deleted)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity {wiki_id!r} not found")
    return _to_out(entity)


@router.delete("/wiki/{type}/{title}", status_code=204)
def delete_entity(
    user_id: str,
    type: str,
    title: str,
    cascade: bool = Query(True, description="Also strip dangling relations other "
                                                "entities held pointing at this one"),
    hard_delete: bool = Query(True, description="Physically remove the file after "
                                                    "tombstoning. False keeps a permanent "
                                                    "tombstone (status=deleted) as an audit trail"),
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    existed = store.delete_entity(user_id, wiki_id, cascade=cascade, hard_delete=hard_delete)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Entity {wiki_id!r} not found")


@router.post("/wiki/{type}/{title}/facts", response_model=EntityOut)
def add_fact(
    user_id: str,
    type: str,
    title: str,
    body: AddFactRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.add_fact(
            user_id, wiki_id, body.text, confidence=body.confidence, evidence=body.evidence
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.patch("/wiki/{type}/{title}/facts/{fact_id}", response_model=EntityOut)
def update_fact(
    user_id: str,
    type: str,
    title: str,
    fact_id: str,
    body: UpdateFactRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.update_fact(
            user_id, wiki_id, fact_id,
            text=body.text, confidence=body.confidence, evidence=body.evidence,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.delete("/wiki/{type}/{title}/facts/{fact_id}", response_model=EntityOut)
def remove_fact(
    user_id: str,
    type: str,
    title: str,
    fact_id: str,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.remove_fact(user_id, wiki_id, fact_id)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.post("/wiki/{type}/{title}/relations", response_model=EntityOut)
def link_entities(
    user_id: str,
    type: str,
    title: str,
    body: LinkEntitiesRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.link_entities(
            user_id, wiki_id, body.target_wiki_id,
            category=body.category, label=body.label, weight=body.weight, reason=body.reason,
            fact_ids=body.fact_ids, evidence=body.evidence, bidirectional=body.bidirectional,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _to_out(entity)


@router.get("/wiki/{type}/{title}/relations", response_model=list[RelationOut])
def get_edges(
    user_id: str,
    type: str,
    title: str,
    category: list[str] | None = Query(None, description="Filter to these relation categories"),
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    edges = store.get_edges(user_id, wiki_id, categories=category)
    return [RelationOut(**r.to_dict()) for r in edges]


@router.patch("/wiki/{type}/{title}/relations/{relation_id}", response_model=EntityOut)
def update_relation(
    user_id: str,
    type: str,
    title: str,
    relation_id: str,
    body: UpdateRelationRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.update_relation(
            user_id, wiki_id, relation_id,
            label=body.label, weight=body.weight, reason=body.reason,
            fact_ids=body.fact_ids, evidence=body.evidence,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.delete("/wiki/{type}/{title}/relations/{relation_id}", response_model=EntityOut)
def remove_relation(
    user_id: str,
    type: str,
    title: str,
    relation_id: str,
    bidirectional: bool = Query(False, description="Also remove the mirrored relation "
                                                       "on the target entity, if one exists"),
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_id = _wiki_id(type, title)
    try:
        entity = store.remove_relation(user_id, wiki_id, relation_id, bidirectional=bidirectional)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.post("/wiki/traverse", response_model=TraverseResponse)
def traverse(
    user_id: str,
    body: TraverseRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    wiki_ids = store.traverse(
        user_id, body.entry_wiki_ids,
        max_depth=body.max_depth, max_nodes=body.max_nodes, categories=body.categories,
    )
    return TraverseResponse(wiki_ids=sorted(wiki_ids))


@router.post("/wiki/subgraph")
def subgraph(
    user_id: str,
    body: SubgraphRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """A drawable neighbourhood: nodes AND the edges between them.

    Use this instead of `GET /wiki` when you want to render or explore the
    graph. Every returned edge has both endpoints in `nodes`, so the result is
    self-contained — paging a graph by index gives you edges pointing at nodes
    you were not sent, which cannot be laid out.

    `hidden_neighbours` per node counts neighbours that were left out, so a UI
    can offer an expand affordance rather than implying a node is a leaf.
    """
    return store.subgraph(
        user_id,
        entry_wiki_ids=body.entry_wiki_ids,
        max_depth=body.max_depth,
        max_nodes=body.max_nodes,
        categories=set(body.categories) if body.categories else None,
    )


@router.post("/wiki/layout")
def set_layout(
    user_id: str,
    body: SetPositionsRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Persist node positions so the force simulation runs once, not on every
    page load, and a node stays where you left it between sessions."""
    positions = {k: (v[0], v[1]) for k, v in body.positions.items() if len(v) >= 2}
    return {"updated": store.manifest.set_positions(user_id, positions)}


@router.post("/wiki/subgraph", response_model=SubgraphResponse)
def subgraph(
    user_id: str,
    body: SubgraphRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """A closed neighbourhood: nodes plus the edges among them.

    Use this instead of `GET /wiki` to draw a graph. Every returned edge has
    both endpoints present, so the result is renderable on its own — which a
    paginated slice of the entity list is not. Measured on a 500-node graph:
    222 KB for the full listing, 3 KB for a depth-2 neighbourhood.
    """
    result = store.subgraph(
        user_id, entry_wiki_ids=body.entry_wiki_ids or None,
        max_depth=body.max_depth, max_nodes=body.max_nodes,
        categories=body.categories)
    return SubgraphResponse(
        nodes=[ManifestEntryOut(**n) for n in result["nodes"]],
        edges=result["edges"], seeds=result["seeds"],
        expandable=result["expandable"], truncated=result["truncated"])


@router.get("/wiki/_layout")
def get_layout(user_id: str, store: EntityGraphStore = Depends(get_graph_store)):
    """Saved node positions, so the graph does not rearrange on every load."""
    return {"positions": store.get_layout(user_id)}


@router.put("/wiki/_layout")
def save_layout(
    user_id: str,
    body: SaveLayoutRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Save node positions. Merged, not replaced."""
    return {"saved": store.save_layout(user_id, body.positions)}


@router.get("/wiki/_stats", response_model=GraphStatsResponse)
def graph_stats(user_id: str, store: EntityGraphStore = Depends(get_graph_store)):
    return GraphStatsResponse(**store.stats(user_id))


@router.post("/wiki/_rebuild_manifest")
def rebuild_manifest(user_id: str, store: EntityGraphStore = Depends(get_graph_store)):
    """Reconstruct the manifest by scanning this user's entity files.

    The manifest is derived state (the entity files are the source of truth,
    ADR-001) and is written back lazily, so a hard crash can leave it stale.
    This is the recovery path that makes that buffering safe to rely on —
    it is the only operation here that does a full directory scan, so it's
    for repair and migration, not the hot path."""
    return {"entries": store.manifest.rebuild(user_id)}


@router.post("/wiki/_compact_ops")
def compact_ops(
    user_id: str,
    on: date = Query(..., description="Day to compact, YYYY-MM-DD. Use closed days only."),
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Fold one day's ops-log segments into a single object.

    Ops are written as immutable segments so appends cost one PUT and no
    read. That keeps writes cheap but leaves a busy day as many small
    objects, which makes read_day() expensive. Run this nightly for the
    previous day."""
    return {"day": on.isoformat(), "records": store.ops_log.compact_day(user_id, on)}


@router.post("/wiki/_reconcile", response_model=ReconcileResponse)
def reconcile(user_id: str, store: EntityGraphStore = Depends(get_graph_store)):
    """Full sweep for dangling relations — safety net for anything the
    per-delete cascade missed (crashes mid-cascade, races). Idempotent;
    safe to call on a schedule or on demand."""
    return ReconcileResponse(**store.reconcile_dangling_relations(user_id))
