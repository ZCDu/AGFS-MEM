"""
Share links: create/list/revoke them from the owner's side, and the guest-
facing "strict" chat that a share link grants.

WHY A SEPARATE, SMALL ENDPOINT INSTEAD OF REUSING /chat
    routes_chat.py's /chat is deliberately general: multi-wiki routing,
    aggregate cross-wiki queries, new-topic wiki proposals, destructive-
    delete confirmation, inline CRUD intents. A guest session must NEVER
    reach any of that surface -- not "is told not to", structurally cannot,
    since the whole point of a share link is a hard boundary. Rebuilding a
    parallel, narrow endpoint that only ever touches ONE owner's store,
    scoped to ONE connected component (app/shares/scope.py), is easier to
    audit for that guarantee than threading a new caller-type through
    /chat's much larger surface would be.

ISOLATION IS STRUCTURAL, NOT A PROMPT INSTRUCTION
    The guest never has their own identity in this code path at all -- there
    is no user_id from a token, only owner_user_id resolved from the share
    record. The extractor is handed owner_user_id directly; nothing here
    could reach any other wiki even if the model tried, and out-of-scope
    writes within the owner's OWN wiki are rejected before apply() runs
    (see restrict_plan_to_scope).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.models import (CreateShareRequest, ShareOut, SharedChatRequest,
                            SharedChatResponse, SharePreview)
from app.auth import require_share, require_user
from app.deps import get_extractor, get_graph_store, get_storage_backend
from app.extract.extractor import ConversationExtractor
from app.graph.store import VALID_TYPES, EntityGraphStore
from app.rawlog.sessions import SessionIdError, SessionLog, new_session_id
from app.shares.scope import component_of, restrict_plan_to_scope
from app.shares.store import ShareLink, ShareLinkStore

logger = logging.getLogger("memory_backend.shares")

owner_router = APIRouter(dependencies=[Depends(require_user)],
                         prefix="/v1/users/{user_id}", tags=["shares"])
guest_router = APIRouter(prefix="/v1/shared", tags=["shares"])

MAX_CONTEXT_ENTITIES = 30

SHARED_SYSTEM_PROMPT = """\
You are a shared note-taking assistant for one specific topic someone chose \
to share with you -- a meeting, a project, a decision, whatever it's about. \
You can see the notes already recorded about it, shown below if there are \
any. You cannot see anything else from the owner's memory, and nothing you \
write here can reach anywhere else, so don't imply otherwise.

When the person you're talking to states something worth remembering about \
THIS topic, note it naturally in your reply -- it will be recorded \
automatically. If they bring up something clearly unrelated to this topic, \
you can still discuss it, but say plainly that it won't be saved here.

Answer normally and conversationally. Do not mention scope, tokens or \
mechanics unless asked."""


def _wiki_id(type_: str, title: str) -> str:
    if type_ not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Unknown type {type_!r} (expected one of {sorted(VALID_TYPES)})")
    return EntityGraphStore.compute_wiki_id(type_, title)


def _share_out(link: ShareLink, request_base: str = "") -> ShareOut:
    return ShareOut(
        share_id=link.share_id, owner_user_id=link.owner_user_id,
        entry_wiki_id=link.entry_wiki_id, label=link.label,
        created_at=link.created_at, expires_at=link.expires_at,
        revoked_at=link.revoked_at, active=link.active,
        url=f"{request_base}/shared/{link.share_id}",
    )


# ---------- owner: create / list / revoke ----------

@owner_router.post("/wiki/{type}/{title}/share", response_model=ShareOut)
def create_share(
    user_id: str, type: str, title: str, body: CreateShareRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Share the connected component this entity belongs to. Anyone with the
    returned URL can open a chat that may add to (never delete from, never
    reach outside) that component -- see app/shares/scope.py."""
    wiki_id = _wiki_id(type, title)
    entity = store.get_entity(user_id, wiki_id, touch=False)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity {wiki_id!r} not found")
    link = ShareLinkStore(get_storage_backend()).create(
        owner_user_id=user_id, entry_wiki_id=wiki_id,
        label=body.label.strip() or entity.title,
        expires_in_days=body.expires_in_days,
    )
    return _share_out(link)


@owner_router.get("/shares", response_model=list[ShareOut])
def list_shares(user_id: str):
    links = ShareLinkStore(get_storage_backend()).list_for_owner(user_id)
    return [_share_out(l) for l in links]


@owner_router.delete("/shares/{share_id}", status_code=204)
def revoke_share(user_id: str, share_id: str):
    store = ShareLinkStore(get_storage_backend())
    link = store.get(share_id)
    if link is None or link.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="No such share link.")
    store.revoke(share_id)


# ---------- guest: preview + strict chat ----------

@guest_router.get("/{share_id}", response_model=SharePreview)
def preview_share(link: ShareLink = Depends(require_share),
                  store: EntityGraphStore = Depends(get_graph_store)):
    entity = store.get_entity(link.owner_user_id, link.entry_wiki_id, touch=False)
    if entity is None:
        # The anchor entity is gone (deleted, merged away). The link still
        # resolves (so the guest gets a clear message, not a bare 404) but
        # grants an empty scope -- component_of() agrees, so chat on it
        # would reject everything anyway.
        return SharePreview(label=link.label, entry_wiki_id=link.entry_wiki_id,
                            entry_title=link.label or link.entry_wiki_id,
                            entry_type="", scope_size=0, active=link.active)
    scope = component_of(store, link.owner_user_id, link.entry_wiki_id)
    return SharePreview(label=link.label, entry_wiki_id=link.entry_wiki_id,
                        entry_title=entity.title, entry_type=entity.type,
                        scope_size=len(scope), active=link.active)


def _scoped_context(store: EntityGraphStore, owner_user_id: str,
                    allowed: set[str]) -> str:
    blocks = []
    for wid in sorted(allowed)[:MAX_CONTEXT_ENTITIES]:
        entity = store.get_entity(owner_user_id, wid, touch=False)
        if entity is None:
            continue
        lines = [f"### {entity.title} ({entity.type})"]
        if entity.summary:
            lines.append(entity.summary)
        for fact in entity.facts[:10]:
            lines.append(f"- {fact.text}")
        for rel in entity.relations[:10]:
            if rel.target in allowed:
                lines.append(f"- {rel.label or rel.category} -> {rel.target}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "Notes recorded so far on this shared topic:\n\n" + "\n\n".join(blocks)


@guest_router.post("/{share_id}/chat", response_model=SharedChatResponse)
def shared_chat(body: SharedChatRequest, link: ShareLink = Depends(require_share),
                extractor: ConversationExtractor = Depends(get_extractor)):
    llm = extractor.llm
    if not llm.configured:
        raise HTTPException(status_code=503,
                            detail="No LLM API key configured. Set DEEPSEEK_API_KEY to enable chat.")

    latest = next((m.content for m in reversed(body.messages) if m.role == "user"), "")
    if not latest.strip():
        raise HTTPException(status_code=422, detail="No user message to reply to.")

    store = extractor.store
    owner = link.owner_user_id
    scope = component_of(store, owner, link.entry_wiki_id)
    if not scope:
        raise HTTPException(
            status_code=410,
            detail="What this link pointed at no longer exists, so there's "
                   "nothing to chat about.")

    context = _scoped_context(store, owner, scope)
    system = SHARED_SYSTEM_PROMPT + ("\n\n" + context if context else "")
    history = body.messages[-20:]
    transcript = "\n\n".join(
        f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" for m in history)

    try:
        reply = llm.complete(system, transcript, json_mode=False, temperature=0.7)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e
    reply = reply.strip()

    # Always attempt extraction -- this endpoint exists specifically so a
    # guest's messages can turn into changes, unlike the general /chat
    # endpoint where that's opt-in. The assessor inside plan() still gates
    # the LLM call, so idle chatter costs nothing extra.
    applied: list[dict] = []
    rejected: list[dict] = []
    try:
        plan = extractor.plan(owner, latest, evidence=f"share:{link.share_id}")
        if plan.operations:
            rejected = restrict_plan_to_scope(plan, scope)
            plan = extractor.apply(owner, plan)
            applied = [o.to_dict() for o in plan.operations if o.status == "applied"]
    except Exception:
        logger.warning("shared-chat extraction failed for share %s", link.share_id,
                       exc_info=True)

    session_id = body.session_id or new_session_id()
    try:
        log = SessionLog(get_storage_backend())
        log.append(owner, session_id, [
            {"role": "user", "content": latest, "via_share": link.share_id},
            {"role": "assistant", "content": reply, "via_share": link.share_id},
        ])
        # Mark both turns examined immediately: extraction (if any) already
        # ran above, scoped to this share. Without this the normal
        # autocapture timer would later re-examine the same text with NO
        # scope restriction at all, defeating the entire point of the link.
        unexamined = log.read_unexamined(owner, session_id)
        if unexamined:
            log.mark_examined(owner, session_id, [i for i, _ in unexamined],
                              by=f"share:{link.share_id}")
    except SessionIdError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception:
        logger.warning("could not log shared session %s", session_id, exc_info=True)

    return SharedChatResponse(reply=reply, session_id=session_id,
                              applied=applied, rejected=rejected)
