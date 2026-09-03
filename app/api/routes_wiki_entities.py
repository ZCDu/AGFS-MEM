"""
Wiki-scoped entity CRUD for collaborative wikis.

The same entity operations as /v1/users/{user_id}/wiki/..., but the entities
live in an arbitrarily-named wiki rather than a user's personal namespace.
This is the endpoint that makes shared wikis real: anyone granted `write` on
`wikis/{wiki_id}` can add entities, facts and relations to it, and anyone
with `read` can read them.

CONTRAST WITH /v1/users/{user_id}/wiki
    The user path answers "the entities whose scope is this user". That was
    the only shape before multi-wiki, and it is kept for compatibility and
    as a shim. This router answers "the entities in THIS wiki", regardless of
    who owns it.

AUTHORISATION
    Every route checks WikiRegistry.require(wiki_id, caller, role):
      - caller identity comes from the bearer token (require_any_credential),
        NOT from the URL — the URL names the wiki, the token names the caller.
      - the platform admin credential (is_admin) bypasses per-wiki grants.
      - write routes require `write` (or `admin`) on the wiki.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.models import (  # noqa: I001
    AddFactRequest,
    EntityOut,
    FactOut,
    LinkEntitiesRequest,
    ManifestEntryOut,
    RelationOut,
    SaveLayoutRequest,
    SubgraphRequest,
    SubgraphResponse,
    TraverseRequest,
    TraverseResponse,
    UpdateFactRequest,
    UpdateRelationRequest,
    UpsertEntityRequest,
)
from app.auth import require_any_credential
from app.deps import get_graph_store
from app.graph.search import rank_entries
from app.graph.store import (VALID_TYPES, Entity, EntityGraphStore,
                             MalformedEntityError, SlugConflictError)
from app.wikis.registry import (ROLE_ADMIN, ROLE_READ, ROLE_WRITE,
                                WikiAccessDenied, WikiNotFound, WikiRegistry)

router = APIRouter(prefix="/v1/wikis/{wiki_id}", tags=["wiki"])


def _registry() -> WikiRegistry:
    from app.deps import get_storage_backend
    return WikiRegistry(get_storage_backend())


def _grant(wiki_id: str, cred: dict, role: str) -> str:
    """Resolve + authorize. Raises HTTPException on failure."""
    is_admin = bool(cred.get("is_admin"))
    caller = cred.get("user_id") or "admin"
    try:
        _registry().require(wiki_id, caller, role, is_admin=is_admin)
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return wiki_id


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


def _refresh_stats(wiki_id: str, store: EntityGraphStore) -> None:
    """Keep the routing summary (entity_count + sample titles) current after
    a write. Best-effort — a stale hint routes worse; a raised error here
    would fail an already-succeeded write."""
    try:
        ids = store.list_entities(wiki_id)
        _registry().refresh_stats(
            wiki_id, len(ids),
            [e.title for e in store.manifest.list_entries(wiki_id)[:25]])
    except Exception:
        import logging
        logging.getLogger("memory_backend.wikis").debug(
            "could not refresh wiki stats for %s", wiki_id, exc_info=True)


# ---------- entity CRUD ----------
# Under /entities to avoid colliding with GET/POST /v1/wikis/{wiki_id} in
# routes_wikis.py, which return the wiki's registry metadata.

@router.put("/entities", response_model=EntityOut)
def upsert_entity(
    wiki_id: str,
    body: UpsertEntityRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Create an entity in this wiki, or update it if it exists."""
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unknown type {body.type!r} (expected one of {sorted(VALID_TYPES)})"
        )
    try:
        entity = store.upsert_entity(
            wiki_id, body.type, body.title,
            aliases=body.aliases, summary_append=body.summary_append,
            compact=body.compact, significance=body.significance,
            on_conflict=body.on_conflict,
        )
    except SlugConflictError:
        raise
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _refresh_stats(wiki_id, store)
    return _to_out(entity)


@router.get("/entities", response_model=list[ManifestEntryOut])
def list_entities(
    wiki_id: str,
    type: str | None = Query(None),
    include_deleted: bool = Query(False),
    q: str | None = Query(None, description="Case-insensitive match on title, aliases, "
                                           "wiki_id, or description (compact) overlap; "
                                           "results are ranked, name matches first"),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_READ)
    entries = store.manifest.list_entries(wiki_id, type_filter=type)
    if not include_deleted:
        entries = [e for e in entries if e.status != "deleted"]
    if q:
        entries = rank_entries(entries, q, store, wiki_id)
    else:
        entries.sort(key=lambda e: (e.type, e.title.lower()))
    if limit is not None:
        entries = entries[offset:offset + limit]
    elif offset:
        entries = entries[offset:]
    return [ManifestEntryOut(**e.to_dict()) for e in entries]


@router.get("/entities/{type}/{title}", response_model=EntityOut)
def get_entity(
    wiki_id: str,
    type: str,
    title: str,
    touch: bool = Query(True),
    include_deleted: bool = Query(False),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_READ)
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    entity = store.get_entity(wiki_id, wiki_entity, touch=touch,
                              include_deleted=include_deleted)
    if entity is None:
        raise HTTPException(status_code=404,
                            detail=f"Entity {wiki_entity!r} not found in wiki {wiki_id!r}")
    return _to_out(entity)


@router.delete("/entities/{type}/{title}", status_code=204)
def delete_entity(
    wiki_id: str,
    type: str,
    title: str,
    cascade: bool = Query(True),
    hard_delete: bool = Query(True),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    existed = store.delete_entity(wiki_id, wiki_entity, cascade=cascade,
                                  hard_delete=hard_delete)
    if not existed:
        raise HTTPException(status_code=404,
                            detail=f"Entity {wiki_entity!r} not found in wiki {wiki_id!r}")
    _refresh_stats(wiki_id, store)


@router.post("/entities/{type}/{title}/facts", response_model=EntityOut)
def add_fact(
    wiki_id: str,
    type: str,
    title: str,
    body: AddFactRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.add_fact(wiki_id, wiki_entity, body.text,
                                confidence=body.confidence, evidence=body.evidence)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.patch("/entities/{type}/{title}/facts/{fact_id}", response_model=EntityOut)
def update_fact(
    wiki_id: str,
    type: str,
    title: str,
    fact_id: str,
    body: UpdateFactRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.update_fact(wiki_id, wiki_entity, fact_id,
                                   text=body.text, confidence=body.confidence,
                                   evidence=body.evidence)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.delete("/entities/{type}/{title}/facts/{fact_id}", response_model=EntityOut)
def remove_fact(
    wiki_id: str,
    type: str,
    title: str,
    fact_id: str,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.remove_fact(wiki_id, wiki_entity, fact_id)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.post("/entities/{type}/{title}/relations", response_model=EntityOut)
def link_entities(
    wiki_id: str,
    type: str,
    title: str,
    body: LinkEntitiesRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.link_entities(
            wiki_id, wiki_entity, body.target_wiki_id,
            category=body.category, label=body.label, weight=body.weight,
            reason=body.reason, fact_ids=body.fact_ids, evidence=body.evidence,
            bidirectional=body.bidirectional,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.get("/entities/{type}/{title}/relations", response_model=list[RelationOut])
def get_edges(
    wiki_id: str,
    type: str,
    title: str,
    category: list[str] | None = Query(None),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_READ)
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    edges = store.get_edges(wiki_id, wiki_entity, categories=category)
    return [RelationOut(**r.to_dict()) for r in edges]


@router.patch("/entities/{type}/{title}/relations/{relation_id}", response_model=EntityOut)
def update_relation(
    wiki_id: str,
    type: str,
    title: str,
    relation_id: str,
    body: UpdateRelationRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.update_relation(
            wiki_id, wiki_entity, relation_id,
            label=body.label, weight=body.weight, reason=body.reason,
            fact_ids=body.fact_ids, evidence=body.evidence,
        )
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


@router.delete("/entities/{type}/{title}/relations/{relation_id}", response_model=EntityOut)
def remove_relation(
    wiki_id: str,
    type: str,
    title: str,
    relation_id: str,
    bidirectional: bool = Query(False),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    wiki_entity = EntityGraphStore.compute_wiki_id(type, title)
    try:
        entity = store.remove_relation(wiki_id, wiki_entity, relation_id,
                                       bidirectional=bidirectional)
    except MalformedEntityError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _to_out(entity)


# ---------- graph-level ----------

@router.post("/subgraph", response_model=SubgraphResponse)
def subgraph(
    wiki_id: str,
    body: SubgraphRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_READ)
    result = store.subgraph(
        wiki_id, entry_wiki_ids=body.entry_wiki_ids,
        max_depth=body.max_depth, max_nodes=body.max_nodes,
        categories=body.categories)
    nodes = result.get("nodes", [])
    expandable = [n["wiki_id"] for n in nodes
                  if n.get("hidden_neighbours", 0) > 0]
    return SubgraphResponse(
        nodes=[ManifestEntryOut(**n) for n in nodes],
        edges=result.get("edges", []), seeds=result.get("seeds", []),
        expandable=expandable, truncated=result.get("truncated", False))


@router.post("/traverse", response_model=TraverseResponse)
def traverse(
    wiki_id: str,
    body: TraverseRequest,
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    _grant(wiki_id, cred, ROLE_READ)
    wiki_ids = store.traverse(
        wiki_id, body.entry_wiki_ids,
        max_depth=body.max_depth, max_nodes=body.max_nodes, categories=body.categories,
    )
    return TraverseResponse(wiki_ids=sorted(wiki_ids))


@router.get("/layout")
def get_layout(wiki_id: str, cred: dict = Depends(require_any_credential),
               store: EntityGraphStore = Depends(get_graph_store)):
    _grant(wiki_id, cred, ROLE_READ)
    return {"positions": store.get_layout(wiki_id)}


@router.put("/layout")
def save_layout(wiki_id: str, body: SaveLayoutRequest,
                cred: dict = Depends(require_any_credential),
                store: EntityGraphStore = Depends(get_graph_store)):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    return {"saved": store.save_layout(wiki_id, body.positions)}


@router.get("/stats")
def graph_stats(wiki_id: str, cred: dict = Depends(require_any_credential),
                store: EntityGraphStore = Depends(get_graph_store)):
    _grant(wiki_id, cred, ROLE_READ)
    return store.stats(wiki_id)


@router.post("/_rebuild_manifest")
def rebuild_manifest(wiki_id: str, cred: dict = Depends(require_any_credential),
                     store: EntityGraphStore = Depends(get_graph_store)):
    _grant(wiki_id, cred, ROLE_ADMIN)
    store.set_actor(cred.get("user_id") or "admin")
    return {"entries": store.manifest.rebuild(wiki_id)}


@router.post("/_reconcile")
def reconcile(wiki_id: str, cred: dict = Depends(require_any_credential),
              store: EntityGraphStore = Depends(get_graph_store)):
    _grant(wiki_id, cred, ROLE_WRITE)
    store.set_actor(cred.get("user_id") or "admin")
    return store.reconcile_dangling_relations(wiki_id)


# ---------- collaboration: activity + membership ----------

@router.get("/activity")
def activity(
    wiki_id: str,
    days: int = Query(7, ge=1, le=90, description="How many trailing days of ops to return"),
    cred: dict = Depends(require_any_credential),
    store: EntityGraphStore = Depends(get_graph_store),
):
    """A feed of who changed what on this wiki, most recent first.

    Read the ops log (ADR-005) for the last `days` days. Every record has an
    `actor` (the user who performed the write), an `op`, a `wiki_id` for the
    entity touched, and a timestamp — that is the "what changed and who did
    it" view a shared team needs. Reads require `read` access, so an
    unauthorised caller cannot see who else is working here.
    """
    _grant(wiki_id, cred, ROLE_READ)
    # WikiOpsLog shards by the UTC date of each write (app/graph/ops_log.py),
    # so the trailing window must anchor on the same UTC "today" -- a naive
    # date.today() (local date) is a day ahead of the log's UTC day for part
    # of every day in any timezone ahead of UTC, which both hides today's
    # freshest ops and pulls in one extra day too far in the past.
    today = datetime.now(timezone.utc).date()
    records: list[dict] = []
    for n in range(days):
        day = today - timedelta(days=n)
        try:
            records.extend(store.ops_log.read_day(wiki_id, day))
        except Exception:
            continue
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"wiki_id": wiki_id, "records": records}


@router.get("/members")
def members(wiki_id: str, cred: dict = Depends(require_any_credential)):
    """Who may read/write this wiki and at what role.

    Only an admin ON THIS WIKI (or platform admin) may list members — knowing
    who else is granted is itself access information.
    """
    is_admin = bool(cred.get("is_admin"))
    caller = cred.get("user_id") or "admin"
    try:
        meta = _registry().require(wiki_id, caller, ROLE_ADMIN, is_admin=is_admin)
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    access = dict(meta.access)
    if is_admin:
        access["admin"] = ROLE_ADMIN  # platform admin sees itself implicitly
    return {
        "wiki_id": meta.wiki_id,
        "title": meta.title,
        "public_read": meta.public_read,
        "members": [{"user_id": u, "role": r} for u, r in sorted(access.items())],
    }
