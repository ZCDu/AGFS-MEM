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
