"""Endpoints for filtered/summarized views of a user's wiki + the diary.

This is the "boss / colleagues" surface:

  - A user (owner) may grant another user a summarized view of their wiki.
  - A colleague with a grant -- or an admin (boss) via oversight -- can ask
    for an on-demand LLM digest of a user's wiki (query / scope bounded).
  - Every such summary is TRANSPARENT: an audit note lands in the owner's
    home wiki, which the owner can list here.
  - The owner (and admin) can read their own daily diary + who-viewed log.

Access model: the caller authorises via the bearer token; summaries never dump
the raw graph by default -- they return a digest with sources. A viewer needs
`can_view(owner, viewer, scope, is_admin)`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_any_credential
from app.config import get_settings
from app.deps import get_graph_store, get_storage_backend
from app.extract.llm import LLMError, LLMNotConfigured
from app.graph.store import EntityGraphStore
from app.views.summarizer import WikiSummarizer
from app.wikis.registry import WikiError, WikiRegistry

logger = logging.getLogger("memory_backend.views")

router = APIRouter(tags=["views"])


def _registry() -> WikiRegistry:
    return WikiRegistry(get_storage_backend())


# Test seam: routes construct a WikiSummarizer via this hook. Tests monkeypatch
# `_summarizer_factory` to inject a fake LLM so the LLM layer is not hit.
_summarizer_factory = None


def _summarizer(store: EntityGraphStore) -> WikiSummarizer:
    if _summarizer_factory is not None:
        return _summarizer_factory(store)
    return WikiSummarizer(_registry(), store)


def _caller(cred: dict) -> str:
    return cred.get("user_id") or "admin"


def _auth_or_403(owner: str, viewer: str, scope: str,
                 is_admin: bool) -> None:
    """Check view authority, raising 403 (never revealing what exists)."""
    reg = _registry()
    if reg.can_view(owner, viewer, scope, is_admin=is_admin):
        return
    if (_registry().get(owner) is None and not is_admin):
        # A nonexistent owner must not be discoverable either.
        raise HTTPException(status_code=404,
                            detail=f"No wiki for user {owner!r}.")
    raise HTTPException(
        status_code=403,
        detail=f"You do not have a summarized-view grant on {owner!r}'s wiki.")


# ---------- grants (owner controls who may summarise their wiki) ----------

class GrantViewRequest(BaseModel):
    viewer: str = Field(..., min_length=1, description="The user to grant a view.")
    scope: str = Field("all", description='"all" | "home" | "topic:{key}".')
    permissions: str = Field("summary", pattern="^(summary|read\\+summary)$")


@router.post("/v1/users/{owner}/wiki/views")
def grant_view(owner: str, body: GrantViewRequest,
               cred: dict = Depends(require_any_credential)):
    """Grant another user a summarized view of your wiki.

    Only the owner (or a platform admin, acting for them) may grant a view of
    the owner's private wiki. `scope` bounds what the viewer may summarise;
    `permissions` bounds whether they also get raw reads.
    """
    caller = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    if caller != owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the owner of a wiki may grant views of it.")
    try:
        return _registry().grant_view(owner, body.viewer, scope=body.scope,
                                      permissions=body.permissions)
    except WikiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/v1/users/{owner}/wiki/views")
def list_views(owner: str, cred: dict = Depends(require_any_credential)):
    caller = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    if caller != owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the owner may list view grants on their wiki.")
    return _registry().list_views(owner)


@router.delete("/v1/users/{owner}/wiki/views/{viewer}")
def revoke_view(owner: str, viewer: str,
                cred: dict = Depends(require_any_credential)):
    caller = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    if caller != owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the owner may revoke view grants on their wiki.")
    removed = _registry().revoke_view(owner, viewer)
    if not removed:
        raise HTTPException(status_code=404, detail="No such view grant.")
    return {"revoked": True, "viewer": viewer}


# ---------- on-demand summarized views ----------

class SummarizeRequest(BaseModel):
    query: str = Field("", description="What the viewer wants to know.")
    scope: str = Field("all", description='"all" | "home" | "topic:{key}".')


@router.post("/v1/users/{owner}/wiki/summarize")
def summarize_owner(owner: str, body: SummarizeRequest,
                    cred: dict = Depends(require_any_credential),
                    store: EntityGraphStore = Depends(get_graph_store)):
    """A colleague (with a view grant) asks for a digest of `owner`'s wiki.

    The digest is LLM-generated and query/scope bounded; it returns a summary
    with sources, NOT a raw graph dump (unless the viewer holds read+summary).
    Every call writes a transparent audit note into the owner's wiki.
    """
    viewer = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    _auth_or_403(owner, viewer, body.scope, is_admin)
    summ = _summarizer(store)
    summ.record_view(owner, viewer, body.scope, kind="summary",
                     note=body.query[:200])
    try:
        return summ.summarize(owner, query=body.query, scope=body.scope,
                              viewer=viewer, is_admin=is_admin)
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/v1/admin/users/{owner}/wiki/summarize")
def admin_summarize(owner: str, body: SummarizeRequest,
                    cred: dict = Depends(require_any_credential),
                    store: EntityGraphStore = Depends(get_graph_store)):
    """A platform admin (boss) summarises any user's wiki (oversight).

    Admins bypass the per-user grant, but the result is still a digest (not a
    raw dump) and the read is still transparent -- an audit note is written
    into the owner's wiki so the employee can see an admin looked.
    """
    if not cred.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="This endpoint requires the platform admin credential.")
    owner_meta = _registry().get(owner)
    if owner_meta is None:
        raise HTTPException(status_code=404, detail=f"No wiki for user {owner!r}.")
    summ = _summarizer(store)
    summ.record_view(owner, "admin", body.scope, kind="admin-summary",
                     note=body.query[:200])
    try:
        return summ.summarize(owner, query=body.query, scope=body.scope,
                              viewer="admin", is_admin=True)
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/v1/admin/users/{owner}/wiki")
def admin_extract(owner: str,
                  cred: dict = Depends(require_any_credential),
                  store: EntityGraphStore = Depends(get_graph_store)):
    """A platform admin extracts a user's RAW wiki data -- every entity, in
    full (facts, aliases, relations), no LLM involved.

    This is the "give me the actual graph" counterpart to admin_summarize's
    LLM digest: exact, free, and works even without an LLM key configured.
    Still transparent -- an audit note is written into the owner's wiki, same
    as a summarize call, so raw extraction is not a quieter back door than
    the digest path.
    """
    if not cred.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="This endpoint requires the platform admin credential.")
    owner_meta = _registry().get(owner)
    if owner_meta is None:
        raise HTTPException(status_code=404, detail=f"No wiki for user {owner!r}.")
    summ = _summarizer(store)
    summ.record_view(owner, "admin", "all", kind="admin-extract", note="raw wiki export")
    entities = []
    for wid in store.list_entities(owner):
        ent = store.get_entity(owner, wid, touch=False)
        if ent is None:
            continue
        entities.append({
            "wiki_id": ent.wiki_id, "type": ent.type, "title": ent.title,
            "aliases": ent.aliases, "compact": ent.compact, "summary": ent.summary,
            "status": ent.status, "merged_into": ent.merged_into,
            "facts": [f.to_dict() for f in ent.facts],
            "relations": [r.to_dict() for r in ent.relations],
            "metadata": ent.metadata.to_dict(),
        })
    return {"owner": owner, "entity_count": len(entities), "entities": entities}


@router.get("/v1/admin/users")
def admin_list_users(cred: dict = Depends(require_any_credential),
                     include_archived: bool = Query(False)):
    """A platform admin lists every wiki (one per user, in this single-wiki
    deployment) so they know who exists without already knowing a user_id.
    """
    if not cred.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="This endpoint requires the platform admin credential.")
    wikis = _registry().list_all(include_archived=include_archived)
    return [{
        "user_id": w.wiki_id,
        "title": w.title,
        "entity_count": w.entity_count,
        "created_at": w.created_at,
        "updated_at": w.updated_at,
        "archived": w.archived,
    } for w in sorted(wikis, key=lambda w: w.wiki_id)]


# ---------- transparency: the owner's own view of what was summarized ----------

@router.get("/v1/users/{owner}/wiki/views/audit")
def view_audit(owner: str, limit: int = Query(50, ge=1, le=500),
               cred: dict = Depends(require_any_credential),
               store: EntityGraphStore = Depends(get_graph_store)):
    """The owner's transparency log: who summarized/viewed my wiki, when.

    Only the owner (or a platform admin) may read this. This is decision a-i:
    oversight is never silent -- the employee can see every summary an admin
    or colleague ran on their wiki.
    """
    viewer = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    if viewer != owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the owner may read the view audit of their wiki.")
    return {"owner": owner,
            "views": _summarizer(store).list_audit(owner, limit=limit)}


# ---------- the scheduled diary ----------

@router.get("/v1/users/{owner}/wiki/diary")
def read_diary(owner: str, day: str | None = Query(None),
               cred: dict = Depends(require_any_credential),
               store: EntityGraphStore = Depends(get_graph_store)):
    """Read a user's diary (owner or platform admin only).

    The diary is produced periodically (by the auto-capture timer) as a
    chronological narrative of the user's actions. The owner reads their own;
    an admin reads any employee's for oversight.
    """
    viewer = _caller(cred)
    is_admin = bool(cred.get("is_admin"))
    if viewer != owner and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only the owner or an admin may read a user's diary.")
    reg = _registry()
    if reg.get(owner) is None and not is_admin:
        raise HTTPException(status_code=404, detail=f"No wiki for user {owner!r}.")
    return {"owner": owner,
            "entries": _summarizer(store).read_diary(owner, day=None)}


@router.post("/v1/admin/users/{owner}/wiki/diary/generate")
def generate_diary_now(owner: str, day: str | None = Query(None),
                       cred: dict = Depends(require_any_credential),
                       store: EntityGraphStore = Depends(get_graph_store)):
    """Manually trigger a diary generation for a user (admin/tooling).

    The scheduled timer calls this automatically each run; this endpoint lets
    an operator force it. Requires platform admin (an employee must not be
    able to fabricate or wipe their own diary)."""
    if not cred.get("is_admin"):
        raise HTTPException(status_code=403,
                            detail="Diary generation requires the platform admin credential.")
    summ = _summarizer(store)
    try:
        record = summ.generate_diary(owner, day=day, viewer="admin")
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if record is None:
        return {"generated": False,
                "note": f"No dated activity for {owner!r} on {day or '(latest)'}."}
    return {"generated": True, "entry": record}
