"""Agent CRUD endpoint — lets the LLM (or any agent) read/write/update/delete
on the entity graph through explicit structured intents.

    POST /v1/users/{user_id}/agent/act

    body: { "wiki_id": optional, "intents": [ {op,...}, ... ] }

Returns one result per intent. This is the "LLM can do all CRUD" surface:
the agent declares exactly what to create/read/update/delete, the backend
executes it through the same store API + permission checks as the REST layer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_user
from app.deps import get_graph_store
from app.graph.store import EntityGraphStore
from app.intents.executor import parse_intents, execute_intents
from app.wikis.registry import (ROLE_WRITE, WikiAccessDenied, WikiNotFound,
                                WikiRegistry)

logger = logging.getLogger("memory_backend.intents_api")

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}", tags=["agent"])


class AgentActRequest(BaseModel):
    wiki_id: str | None = Field(
        None, description="Default wiki for intents that don't name one.")
    # Accept either raw intents or a chat-like reply (with <intents> block).
    intents: list[dict] | None = Field(
        None, description="Explicit structured CRUD intents.")
    reply_text: str | None = Field(
        None, description="A model reply containing an <intents>...</intents> "
                          "block; parsed into intents.")
    confirm: bool = Field(
        True, description="When true (default), write intents are applied. "
                          "Set false to dry-run (validate + return, no writes).")


class AgentActResponse(BaseModel):
    results: list[dict]
    applied: int
    failed: int
    skipped: int


@router.post("/agent/act", response_model=AgentActResponse)
def agent_act(user_id: str, body: AgentActRequest,
              store: EntityGraphStore = Depends(get_graph_store)):
    """Execute explicit CRUD intents (or parse them from a reply) against the
    user's memory graph."""
    intents: list[dict] = body.intents or []
    if not intents and body.reply_text:
        intents = parse_intents(body.reply_text)

    if not intents:
        return AgentActResponse(results=[], applied=0, failed=0, skipped=0)

    # Resolve which wikis the agent can WRITE to.
    registry = WikiRegistry(store.backend) if hasattr(store, "backend") else None
    if registry is None:
        from app.deps import get_storage_backend
        registry = WikiRegistry(get_storage_backend())

    def writer_for(wiki_id: str) -> bool:
        try:
            registry.require(wiki_id, user_id, ROLE_WRITE)
            return True
        except (WikiNotFound, WikiAccessDenied):
            return False

    # Apply the default wiki to any intent that omitted one.
    for intent in intents:
        if "wiki" not in intent and body.wiki_id:
            intent["wiki"] = body.wiki_id

    # Handle create_wiki intents here (needs registry materialisation, which
    # the store executor cannot do). Others go to the store executor.
    executable: list[dict] = []
    results: list[dict] = []
    for intent in intents:
        op = intent.get("op", "")
        if op != "create_wiki":
            executable.append(intent)
            continue
        try:
            title = (intent.get("title") or "New Wiki").strip()
            wiki = registry.create(
                title, created_by=user_id,
                description=intent.get("description") or "",
                topic=intent.get("topic") or "",
            )
            try:
                registry.grant(wiki.wiki_id, user_id, ROLE_WRITE)
            except Exception:
                pass  # creator is already admin
            results.append({"op": "create_wiki", "wiki": wiki.wiki_id,
                            "status": "applied", "message": f"wiki {wiki.title!r} created"})
        except Exception as e:
            results.append({"op": "create_wiki", "wiki": "",
                            "status": "failed", "message": f"{type(e).__name__}: {e}"})

    if not body.confirm:
        # Dry-run: validate each intent's shape + write access without applying.
        for intent in executable:
            op = intent.get("op", "")
            wiki = intent.get("wiki") or user_id
            ok = writer_for(wiki)
            results.append({
                "op": op, "wiki": wiki,
                "status": "would-apply" if ok else "no-write-access",
                "message": "dry-run (confirm=false)",
            })
        return AgentActResponse(
            results=results,
            applied=sum(1 for r in results if r["status"] == "would-apply"),
            failed=sum(1 for r in results if r["status"] != "would-apply"),
            skipped=0,
        )

    results += execute_intents(store, user_id, executable, writer_for=writer_for)
    return AgentActResponse(
        results=results,
        applied=sum(1 for r in results if r["status"] == "applied"),
        failed=sum(1 for r in results if r["status"] == "failed"),
        skipped=sum(1 for r in results if r["status"] == "skipped"),
    )


# ---------------------------------------------------------------------------
# Confirm-before-forget: staged destructive deletions
# ---------------------------------------------------------------------------
# A pure "forget X" deletes a whole entity node. It is staged (needs_confirmation)
# rather than auto-applied; these endpoints list the pending deletions and apply
# one once the user confirms.

@router.get("/deletions/pending")
def list_pending(user_id: str,
                 store: EntityGraphStore = Depends(get_graph_store)):
    """List staged, not-yet-confirmed node deletions for this user."""
    from app.intents.semantica_crud import list_pending_deletions
    return {"user_id": user_id,
            "pending": list_pending_deletions(store.backend, user_id)}


# ---- TEMP / THROWAWAY (2026-08-28) -------------------------------------------------
# Read-only view of Semantica conflict records, so conflicts can be inspected
# while testing. Add nothing else here; REMOVE this block once testing is done.
@router.get("/conflicts")
def list_conflicts_temp(user_id: str, wiki: str | None = None,
                        type_: str | None = None,
                        status: str | None = None,
                        store: EntityGraphStore = Depends(get_graph_store)):
    """[TEMP] List Semantica conflict records for a wiki scope."""
    scope = wiki or user_id
    recs = store.list_conflicts(scope, type_=type_, status=status)
    return {"wiki": scope, "count": len(recs), "conflicts": recs}


@router.post("/deletions/{pending_id}/confirm")
def confirm_deletion(user_id: str, pending_id: str,
                     store: EntityGraphStore = Depends(get_graph_store)):
    """Apply a staged node deletion that the user has confirmed. One-shot: the
    pending record is consumed whether or not the delete succeeds."""
    from app.intents.semantica_crud import confirm_pending_deletion
    from app.wikis.registry import WikiRegistry

    registry = WikiRegistry(store.backend) if hasattr(store, "backend") else None
    if registry is None:
        from app.deps import get_storage_backend
        registry = WikiRegistry(get_storage_backend())

    def writer_for(wiki_id):
        try:
            registry.require(wiki_id, user_id, ROLE_WRITE)
            return True
        except (WikiNotFound, WikiAccessDenied):
            return False

    return confirm_pending_deletion(store, user_id, writer_for, pending_id)


@router.post("/deletions/{pending_id}/cancel")
def cancel_deletion(user_id: str, pending_id: str,
                    store: EntityGraphStore = Depends(get_graph_store)):
    """Abandon a staged deletion WITHOUT executing it; the entity stays."""
    from app.intents.semantica_crud import cancel_pending_deletion
    return cancel_pending_deletion(store.backend, user_id, pending_id)


# Alias for discoverability.
router.add_api_route(
    "/agent/act/dryrun", agent_act, methods=["POST"],
    response_model=AgentActResponse, include_in_schema=True,
    description="Dry-run of /agent/act: validate intents without writing.",
)
