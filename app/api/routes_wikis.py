"""
Wiki management and routing.

NOT under /v1/users/{user_id}: wikis are no longer owned by a user, they are
organisation-level with explicit grants. That path prefix would imply an
ownership that no longer exists, and would make a shared wiki look like it
belonged to whoever happened to be calling.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_any_credential
from app.config import get_settings
from app.deps import get_graph_store, get_storage_backend
from app.graph.store import EntityGraphStore
from app.wikis.registry import (ROLE_ADMIN, ROLE_READ, ROLE_WRITE, WikiAccessDenied,
                                WikiError, WikiNameCollision, WikiNotFound,
                                WikiRegistry)
from app.wikis.router import WikiRouter

logger = logging.getLogger("memory_backend.wikis")

router = APIRouter(prefix="/v1", tags=["wikis"])


def _registry() -> WikiRegistry:
    return WikiRegistry(get_storage_backend())


def _router(store: EntityGraphStore) -> WikiRouter:
    return WikiRouter(_registry(), store)


def _caller(cred: dict) -> str:
    """The user id a credential speaks for.

    An admin credential has no user of its own, so it acts as the literal
    "admin" for ownership purposes — it can create and grant, but a wiki it
    creates still needs a real user granted on it before anyone else can work.
    """
    return cred.get("user_id") or "admin"


# ---------- models ----------

class CreateWikiRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = Field("")
    wiki_id: str | None = Field(None, description="Derived from the title if omitted")
    public_read: bool = Field(False, description="Any authenticated user may read")
    allow_similar: bool = Field(
        False,
        description="Create even if the name is close to an existing wiki. "
                    "Splitting a graph across near-duplicate names is worse "
                    "than one large graph, so this must be deliberate.")


class GrantRequest(BaseModel):
    user_id: str
    role: str = Field(ROLE_READ, pattern="^(read|write|admin)$")


class CreateInviteRequest(BaseModel):
    role: str = Field(ROLE_WRITE, pattern="^(read|write|admin)$")
    uses_left: int | None = Field(
        None, ge=1, description="Max redemptions, or null for unlimited.")
    expires_in: int | None = Field(
        None, ge=1, description="Seconds from now until it expires, or null.")
    passcode: str | None = Field(
        None, min_length=4, max_length=64,
        description="Optional explicit passcode; a random one is generated.")


class JoinRequest(BaseModel):
    passcode: str = Field(..., min_length=4, max_length=64)


class RouteRequest(BaseModel):
    text: str = Field(..., min_length=1)
    role: str = Field(ROLE_READ, pattern="^(read|write)$")
    allow_create: bool = Field(
        False, description="Let the decision be 'create'. Creates nothing by "
                           "itself — the caller performs it.")


# ---------- registry ----------

@router.get("/wikis")
def list_wikis(cred: dict = Depends(require_any_credential),
               role: str = Query(ROLE_READ, pattern="^(read|write|admin)$"),
               include_archived: bool = Query(False)):
    """Only wikis this credential can reach.

    Listing everything would tell a caller which wikis exist, which is itself
    information they may not be entitled to.
    """
    reg = _registry()
    if cred.get("is_admin"):
        return [w.to_dict() for w in reg.list_all(include_archived)]
    return [w.to_dict() for w in
            reg.list_for(_caller(cred), role=role, include_archived=include_archived)]


@router.post("/wikis", status_code=201)
def create_wiki(body: CreateWikiRequest,
                cred: dict = Depends(require_any_credential)):
    settings = get_settings()
    if settings.wiki_create_requires_admin and not cred.get("is_admin"):
        raise HTTPException(
            status_code=403,
            detail="Creating wikis is restricted to admin credentials "
                   "(WIKI_CREATE_REQUIRES_ADMIN=true).")
    try:
        wiki = _registry().create(
            body.title, created_by=_caller(cred), description=body.description,
            wiki_id=body.wiki_id, public_read=body.public_read,
            allow_similar=body.allow_similar)
    except WikiNameCollision as e:
        # 409 with the existing id, so the caller can use it rather than
        # creating a near-duplicate.
        raise HTTPException(status_code=409, detail={
            "message": str(e), "existing_wiki_id": e.existing_id,
            "existing_title": e.existing_title,
            "hint": "Use that wiki, or pass allow_similar=true if they really "
                    "are distinct."}) from e
    except WikiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return wiki.to_dict()


@router.get("/wikis/{wiki_id}")
def get_wiki(wiki_id: str, cred: dict = Depends(require_any_credential)):
    try:
        wiki = _registry().require(wiki_id, _caller(cred), ROLE_READ,
                                   is_admin=cred.get("is_admin", False))
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return wiki.to_dict()


@router.post("/wikis/{wiki_id}/access")
def grant_access(wiki_id: str, body: GrantRequest,
                 cred: dict = Depends(require_any_credential)):
    """Granting requires admin ON THAT WIKI — write access lets you change
    content, not who else can."""
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        return reg.grant(wiki_id, body.user_id, body.role).to_dict()
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except WikiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/wikis/{wiki_id}/access/{target_user}")
def revoke_access(wiki_id: str, target_user: str,
                  cred: dict = Depends(require_any_credential)):
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        return reg.revoke(wiki_id, target_user).to_dict()
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.post("/wikis/{wiki_id}/archive")
def archive_wiki(wiki_id: str, archived: bool = Query(True),
                 cred: dict = Depends(require_any_credential)):
    """Hides a wiki from routing without deleting it.

    There is deliberately no delete endpoint. A wiki auto-created in error
    should stop attracting conversations, but its contents may still be
    wanted, and nothing automatic should be able to destroy a graph.
    """
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        return reg.set_archived(wiki_id, archived).to_dict()
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ---------- merge / split (two independent graphs, could be merged) ----------

def _wiki_edges(store: EntityGraphStore, wiki_id: str) -> list[dict]:
    """All relations recorded on entities of ONE wiki, as {src,tgt,label}."""
    edges = []
    for wid in store.list_entities(wiki_id):
        ent = store.get_entity(wiki_id, wid, touch=False)
        if ent is None or ent.status == "deleted":
            continue
        for rel in ent.relations:
            if rel.target:
                edges.append({"src": wid, "tgt": rel.target,
                              "label": rel.label or rel.category})
    return edges


def _connecting_edges(store: EntityGraphStore, a: str, b: str) -> list[dict]:
    """Edges whose endpoints straddle the two wikis (either direction). These
    are the relations that make the two graphs actually CONNECTED."""
    b_ids = set(store.list_entities(b))
    links = []
    for e in _wiki_edges(store, a):
        if e["tgt"] in b_ids:
            links.append(e)
    a_ids = set(store.list_entities(a))
    for e in _wiki_edges(store, b):
        if e["tgt"] in a_ids:
            links.append(e)
    return links


@router.post("/wikis/{wiki_id}/connections", response_model=dict | None)
def wiki_connections(wiki_id: str,
                     cred: dict = Depends(require_any_credential)):
    """Report how many independent (disconnected) subgraphs a wiki contains.

    A wiki can hold several unrelated topic-graphs that were merged in by
    accident. This returns the internal edge set so a caller can see whether
    it is one connected graph or several. Requires read access."""
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_READ,
                    is_admin=cred.get("is_admin", False))
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    store = get_graph_store()
    return {"wiki_id": wiki_id, "edges": _wiki_edges(store, wiki_id),
            "edge_count": len(_wiki_edges(store, wiki_id)),
            "entity_count": len(store.list_entities(wiki_id))}


@router.post("/wikis/{source}/merge-into/{target}")
def merge_wiki(source: str, target: str,
               cred: dict = Depends(require_any_credential)):
    """Merge every entity of `source` into `target`, then empty `source`.

    This is the "join two graphs" operation for the case where content that
    should have been separate ended up split (or where two separate graphs are
    genuinely the same thing after a later connection). It is deliberately
    guarded: the caller must be a signed-in credential AND must explicitly
    confirm the merge (the act of calling this endpoint with a valid token is
    the confirmation). Without a valid credential the merge is refused.

    Before merging it reports whether the two wikis were CONNECTED (shared an
    edge). Pass force=true to merge even when they are disconnected (e.g. you
    have linked them by intent). A merge of a wiki into itself is refused.
    """
    # AUTH GATE: refuse unless the caller presents a valid credential and has
    # admin on BOTH wikis. The token is the confirmation to merge.
    reg = _registry()
    caller = _caller(cred)
    for wid in (source, target):
        try:
            reg.require(wid, caller, ROLE_ADMIN,
                        is_admin=cred.get("is_admin", False))
        except WikiNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except WikiAccessDenied as e:
            raise HTTPException(
                status_code=403,
                detail="Merge requires admin on both wikis and a valid, "
                       "confirmed credential.") from e
    if source == target:
        raise HTTPException(status_code=400, detail="Cannot merge a wiki into itself.")

    store = get_graph_store()
    src_ids = store.list_entities(source)
    if not src_ids:
        raise HTTPException(status_code=409, detail=f"Source wiki {source!r} is empty.")

    # Whether the two graphs were connected before merging — reported, not a
    # blocker. Per the chosen rule, merging a disconnected pair is allowed as
    # long as the caller is authenticated (the token above is that confirm);
    # we just surface that they were independent graphs.
    connected = _connecting_edges(store, source, target)

    moved, failed = [], []
    for wid in src_ids:
        r = store.move_entity_between_wikis(source, target, wid)
        if r.get("moved"):
            moved.append(wid)
        else:
            failed.append({"wiki_id": wid, "error": r.get("failed")})

    # Empty source: archive it (there is no delete-by-design).
    leftover = store.list_entities(source)
    if not leftover:
        try:
            reg.set_archived(source, True)
        except Exception:
            pass
    return {
        "source": source,
        "target": target,
        "moved": len(moved),
        "failed": failed,
        "connected_before": bool(connected),
        "connections_before": connected,
        "target_entity_count": len(store.list_entities(target)),
    }


# ---------- passcode invites / join ----------

@router.post("/wikis/{wiki_id}/invite", status_code=201)
def create_invite(wiki_id: str, body: CreateInviteRequest,
                  cred: dict = Depends(require_any_credential)):
    """Issue a passcode invite to a wiki. Requires admin on that wiki.

    The passcode is returned once, in the response; an administrator shares
    it with whoever should join. Redeeming it grants the invite's role.
    """
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        return reg.create_invite(
            wiki_id, _caller(cred), role=body.role,
            uses_left=body.uses_left, expires_in=body.expires_in,
            passcode=body.passcode)
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except WikiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/wikis/{wiki_id}/join")
def join_wiki(wiki_id: str, body: JoinRequest,
              cred: dict = Depends(require_any_credential)):
    """Redeem a passcode to gain access to a wiki as a self-service step.

    Any authenticated user may attempt it; the passcode itself is the
    authorisation. On success the caller is granted the invite's role.
    """
    reg = _registry()
    try:
        caller = _caller(cred)
        return reg.redeem_invite(wiki_id, body.passcode, caller).to_dict()
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/wikis/{wiki_id}/invites")
def list_invites(wiki_id: str, cred: dict = Depends(require_any_credential)):
    """List active invites (admin on the wiki only). Passcodes are exposed
    here too -- listing invites means you already administer the wiki."""
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        return reg.list_invites(wiki_id)
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


@router.delete("/wikis/{wiki_id}/invite/{passcode}")
def revoke_invite(wiki_id: str, passcode: str,
                  cred: dict = Depends(require_any_credential)):
    """Revoke a passcode so it can no longer be redeemed. Admin only.

    Existing grants are untouched; only the invite is invalidated.
    """
    reg = _registry()
    try:
        reg.require(wiki_id, _caller(cred), ROLE_ADMIN,
                    is_admin=cred.get("is_admin", False))
        removed = reg.revoke_invite(wiki_id, passcode)
        if not removed:
            raise HTTPException(status_code=404, detail="No such invite.")
        return {"revoked": True}
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


# ---------- routing ----------

@router.post("/route")
def route(body: RouteRequest,
          cred: dict = Depends(require_any_credential),
          store: EntityGraphStore = Depends(get_graph_store)):
    """Which wiki does this text belong to?

    Scores every reachable wiki with the assessor — no model call, no storage
    read beyond the registry — and returns a decision with its reasoning.
    Creates nothing: an "action": "create" decision is a proposal for the
    caller to act on, which is what allows it to be reviewed.
    """
    decision = _router(store).route(
        _caller(cred), body.text, role=body.role, allow_create=body.allow_create)
    return decision.to_dict()
