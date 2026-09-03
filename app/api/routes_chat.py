"""
Chat, with the user's memory used as context.

The point of the memory system is that the assistant knows things. A chat
endpoint that just proxies to DeepSeek would not exercise any of it, so this
retrieves before it answers:

    message -> find related entities -> inject as context -> LLM -> reply

Retrieval reuses ConversationAssessor's linking, which matches the message
against titles, aliases and compact summaries in the manifest. That costs one
manifest read (normally already in memory) and no entity reads, so adding
memory to a turn is close to free. It is deliberately the same code that
decides what is worth STORING — if it cannot find an entity to link a
statement to, it will not find it to answer with either, and that symmetry
makes retrieval failures visible.

STATELESS
    The full message list is sent by the client on every turn. No server-side
    session store, so nothing to expire, share between workers, or clean up.
    The cost is bandwidth on long conversations, which is bounded by
    CHAT_HISTORY_TURNS.

WHAT THIS DOES NOT DO
    It does not store anything. Extraction is a separate, explicit step
    (/extract then /extract/apply) because a model deciding on its own what to
    write into long-term memory, mid-conversation, is how memory silently
    fills with hallucinated facts.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_user
from app.config import get_settings
from app.deps import get_graph_store, get_storage_backend
from app.extract.llm import LLMClient, LLMNotConfigured
from app.graph.store import EntityGraphStore
from app.rawlog.files import FileError, FileStore, file_ref, parse_file_ref
from app.rawlog.sessions import SessionIdError, SessionLog, new_session_id
from app.intents.executor import strip_intents
from app.verify.assessor import ConversationAssessor
from app.wikis.registry import (ROLE_READ, ROLE_WRITE, WikiAccessDenied,
                                WikiError, WikiNotFound, WikiRegistry,
                                slugify_wiki)

logger = logging.getLogger("memory_backend.chat")

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}", tags=["chat"])

# How many prior turns to send. Long enough for continuity, short enough that
# a long session does not quietly become an expensive one.
CHAT_HISTORY_TURNS = 20
MAX_CONTEXT_ENTITIES = 8

SYSTEM_PROMPT = """You are a helpful assistant with access to the user's long-term memory.

When memory is provided below, use it: refer to people, projects and decisions \
by the names recorded there, and treat recorded facts as things you already \
know rather than things the user just told you.

If the memory does not cover something, say so plainly instead of inventing \
detail. Never state a fact about the user's world that is not either in the \
memory or in this conversation.

Answer normally and conversationally. Do not mention that you are consulting \
a memory system unless asked."""


TOOLS_RUBRIC = """\
You have the ability to UPDATE the user's long-term memory. This is a \
classification task. For EACH user message you must decide: does the user \
want memory to change? If yes, you MUST emit an intent block. If you reply \
\"Got it\" / \"I'll keep that in mind\" / \"I'll remember that\" without an \
intent block, you have FAILED to update memory and the user's stated change \
will be lost. Never acknowledge a memory change in prose without the matching \
intent.

DECISION CRITERIA -- these all mean EMIT an intent:
  - User states a new fact about a person/project/decision   -> add_fact or create_entity
  - "remember X", "note X"                                   -> add_fact
  - "forget X", "remove X", "delete X"                      -> delete_fact or delete_entity
  - "X is no longer true", "X no longer ...", "I no longer ..."  -> update_fact (or delete_fact if it is now obsolete)
  - "X is wrong", "that was wrong", "I shouldn't have said X",
    "actually it's Y not X", "correction: ..."               -> update_fact (edit the old value) or delete_fact (remove it)
  - "I take that back", "scratch that", "ignore the last thing" -> delete_fact / delete_entity / update_fact

EXAMPLES:
  User: "Sarah no longer leads the Azure migration." -> this is a CHANGE. Say who leads now if stated; emit an update_fact changing the lead fact.
  User: "The deadline is wrong, it's October not September." -> emit an update_fact with the corrected deadline.
  User: "Maria isn't researching the coffee machine anymore." -> emit an update_fact or delete_fact for that fact.

EMIT FORMAT -- put this AFTER your prose reply:
<intents>
[{"op":"update_fact","wiki":"<wiki>","entity":"person/sarah","fact_id":"<id>","text":"<corrected>"}]
</intents>

Supported ops:
  create_entity{type,title,summary,aliases}  update_entity{entity,summary,aliases}
  delete_entity{entity}  add_fact{entity,text,confidence}  update_fact{entity,fact_id,text[,confidence]}
  delete_fact{entity,fact_id}  add_relation{source,target,category,label}
  update_relation{source,relation_id,label}  delete_relation{source,relation_id}
  create_wiki{title,description,topic}

If the user is merely asking a question or chatting (not changing memory), \
emit NO intents. Use entity ids like person/sarah-kim when you know them."""


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    session_id: str | None = Field(
        None, description="Omit on the first turn; the id is returned and "
                          "should be sent on subsequent turns so the whole "
                          "conversation lands in one session file.")
    wiki_id: str | None = Field(
        None, description="Which knowledge graph to answer from. Omitted, the "
                          "router picks one and reports which in the response.")
    attachments: list[str] = Field(
        default_factory=list,
        description="File references from POST /files, as 'file:YYYY-MM-DD:id' "
                    "or a bare file id. Their text is given to the model and "
                    "the reference is journalled.")
    log: bool = Field(True, description="Persist this turn to the session log")
    use_memory: bool = Field(True, description="Retrieve and inject related entities")
    tools_enabled: bool = Field(
        False, description="When true, the model may emit a structured "
                           "<intents>...</intents> block in its reply to "
                           "create/read/update/delete entities, facts and "
                           "relations on the graph. Executed after the reply "
                           "with write-permission checks. Off by default so "
                           "the read-only chat behaviour is preserved unless "
                           "a deployment opts in.")
    temperature: float = Field(0.7, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    reply: str
    context_used: list[dict]
    memory_hit: bool
    session_id: str | None = None
    wiki_id: str | None = None
    wiki_reason: str = ""
    new_wiki_proposal: dict | None = Field(
        None, description="When the message is a genuinely new topic with no "
                          "matching wiki, an LLM-proposed {title, description, topic, reason} "
                          "for a NEW wiki. Nothing is created; the UI offers "
                          "this as a proposal the user can confirm.")
    tool_results: list[dict] | None = Field(
        None, description="When tools_enabled, the outcomes of any CRUD intents "
                          "the model issued in its reply. One entry per intent.")


def _client() -> LLMClient:
    s = get_settings()
    return LLMClient(api_key=s.llm_api_key, base_url=s.llm_base_url,
                     model=s.llm_model, timeout=s.llm_timeout_seconds)


def _memory_context(store: EntityGraphStore, user_id: str,
                    message: str) -> tuple[str, list[dict]]:
    """Entities related to the message, rendered for the prompt.

    Reads full entity files only for what actually matched — the assessor
    works off the manifest, so an unrelated message costs no entity reads at
    all.
    """
    # min_words=1 deliberately. The assessor's length gate exists to stop
    # trivial chatter being STORED, and it returns early with related=[] —
    # but questions are short by nature ("What is Alice working on?" is six
    # words) and retrieval does not care whether the text is worth keeping,
    # only what it refers to. Reusing the storage thresholds here made every
    # short question retrieve nothing, which is most of them.
    assessment = ConversationAssessor(store, min_words=1).assess(
        user_id, message, link_only=True)
    used: list[dict] = []
    blocks: list[str] = []

    # Disconnected clusters within one wiki get no special treatment by
    # default -- a query ambiguous between two unrelated clusters would
    # otherwise just take whatever the flat per-entity confidence produced.
    # Soft-prioritize (reorder, never drop) toward the cluster whose
    # entities score more relevant overall -- see app/graph/components.py
    # for why this is provably a no-op on a single-cluster wiki (the common
    # case) rather than merely usually-a-no-op.
    ranked = assessment.related
    if len(assessment.related) > 1:
        from app.graph.components import (build_adjacency,
                                          component_priority_weights,
                                          connected_components)
        entries = store.manifest.list_entries(user_id)
        adj = build_adjacency(entries)
        comps = connected_components(adj)
        scores = {r.wiki_id: r.confidence for r in assessment.related}
        weights = component_priority_weights(comps, scores)
        ranked = sorted(assessment.related,
                        key=lambda r: -(r.confidence * weights.get(r.wiki_id, 1.0)))

    for link in ranked[:MAX_CONTEXT_ENTITIES]:
        try:
            entity = store.get_entity(user_id, link.wiki_id, touch=True)
        except Exception:
            continue
        if entity is None:
            continue
        lines = [f"### {entity.title} ({entity.type})"]
        if entity.summary:
            lines.append(entity.summary)
        for fact in entity.facts[:10]:
            lines.append(f"- {fact.text}")
        for rel in entity.relations[:10]:
            lines.append(f"- {rel.label or rel.category} -> {rel.target}")
        blocks.append("\n".join(lines))
        used.append({"wiki_id": entity.wiki_id, "title": entity.title,
                     "type": entity.type, "matched_on": link.matched_on,
                     "confidence": link.confidence})

    if not blocks:
        return "", []
    return "Relevant memory:\n\n" + "\n\n".join(blocks), used


# Words that mark a READ as spanning multiple wikis: "everything", "all
# projects", "open items", "summary", "overview", "across", "deadlines".
_AGGREGATE_TOKENS = (
    "all projects", "our projects", "every project", "across",
    "everything", "summary", "overview", "open items", "what's open",
    "whats open", "deadlines", "decisions", "active work", "on our plate",
    "status update", "status of everything", "spanning", "any project",
)


def _is_aggregate_query(text: str) -> bool:
    """True when a question looks like it wants an overview across wikis
    rather than an answer from a single wiki."""
    low = (text or "").lower()
    return any(tok in low for tok in _AGGREGATE_TOKENS)


def _is_identity_wiki(wiki) -> bool:
    """True for personal-profile / onboarding wikis that add little to a
    cross-project OVERVIEW question but crowd out real facts. Detected by
    topic/description keyed on identity-ish terms. A user's own profile page
    ("Miguel", "Miguel Fu") is not relevant to "what's open across projects"."""
    hay = " ".join([
        (getattr(wiki, "topic", "") or ""),
        (getattr(wiki, "title", "") or ""),
        (getattr(wiki, "description", "") or ""),
    ]).lower()
    # Unambiguous onboarding / self-introduction wikis only. The generic word
    # "profile" is far too broad: "Profile of Anna, a worker at Taldic Corps"
    # is a real person entity wiki (Anna), not the caller's own page, and
    # skipping it breaks cross-wiki lookup (".whos Anna"). Fire only on
    # signals that mark a self-intro / onboarding page.
    return (
        any(term in hay for term in (
            "onboarding", "user introduction", "user profile",
            "about me", "my introduction", "personal introduction",
        ))
        # A topic literally named something like "marcus hale profile" with a
        # person's own name as the page subject is a person wiki, not onboarding.
        and not _topic_is_person_entity(wiki)
    )


def _topic_is_person_entity(wiki) -> bool:
    """True when a wiki's topic looks like a specific proper-noun person subject
    ("Anna", "Dmitri Volkov", "Priya Nair") rather than a generic onboarding
    page ("User introduction", "About me"). Used to keep real person entity
    wikis out of the onboarding skip-list so they stay retrievable by name.

    A generic/onboarding topic reads as a *concept* ("introduction",
    "onboarding", "profile", "about"); a person-subject topic reads as a
    *name* (capitalised, no generic topic words). If any generic topic word is
    present we treat it as non-specific so the onboarding detection can fire."""
    topic = (getattr(wiki, "topic", "") or "") or (getattr(wiki, "title", "") or "")
    words = [w for w in topic.split() if w]
    if not words:
        return False
    # A person subject is short and starts with a capitalised word (a name).
    first = words[0]
    if not (first[:1].isupper() if first else False):
        return False
    if len(words) > 3:
        return False  # too long to be a bare name; it's a topic phrase
    generic = {
        "introduction", "onboarding", "profile", "about", "user", "personal",
        "me", "my", "overview", "summary", "update", "team", "project",
        "for", "and", "of", "the", "role", "status", "worker", "manager",
        "planning", "management", "migration", "data", "center", "corps",
    }
    return not any(w.lower() in generic for w in words)


def _can_read(registry: WikiRegistry, wiki_id: str, user_id: str) -> bool:
    """Whether the caller may read a wiki. In open/auth-off mode grants are
    empty so list_for() returns nothing even though every wiki is reachable; we
    enumerate list_all() and re-check per-wiki access here instead. Archived
    wikis are already excluded by list_all(include_archived=False)."""
    try:
        registry.require(wiki_id, user_id, ROLE_READ)
        return True
    except (WikiNotFound, WikiAccessDenied):
        return False


def _aggregate_memory_context(store: EntityGraphStore, user_id: str,
                              registry: WikiRegistry,
                              question: str = "") -> tuple[str, list[dict]]:
    """Retrieve context from EVERY non-personal wiki the user can read, merging
    them, so an OVERVIEW question gets a real cross-wiki picture.

    Filters two ways to avoid polluting the answer with useless identity
    records:
      (1) SKIP personal-profile / onboarding wikis -- "Miguel"/"Miguel Fu"
          belong to those and are irrelevant to a cross-project overview.
      (2) TOPICAL: when a question is given, only keep entities whose content
          shares vocabulary with the question, so a chatty wiki cannot drown
          the real facts. Same content-token overlap the router uses.
    """
    wikis = [w for w in registry.list_all(include_archived=False)
             if not _is_identity_wiki(w) and _can_read(registry, w.wiki_id, user_id)]
    q_toks = _content_tokens(question) if question else None
    blocks: list[str] = []
    used: list[dict] = []
    for w in wikis:
        try:
            ctx, used_here = _memory_context(store, w.wiki_id, question or "")
        except Exception:
            continue
        if not used_here:
            try:
                entries = store.list_entities(w.wiki_id)
            except Exception:
                continue
            picked = entries[:MAX_CONTEXT_ENTITIES] if entries else []
            sub: list[str] = []
            for wid in picked:
                entity = store.get_entity(w.wiki_id, wid, touch=True)
                if entity is None:
                    continue
                # Topical filter: skip entities that share no content words
                # with the question (unless no question was given).
                if q_toks:
                    ent_toks = _content_tokens(
                        " ".join([entity.title or "",
                                  entity.compact or "",
                                  entity.summary or ""]))
                    if not (ent_toks & q_toks):
                        continue
                lines = [f"### {entity.title} ({entity.type})"]
                if entity.summary:
                    lines.append(entity.summary)
                for fact in entity.facts[:6]:
                    lines.append(f"- {fact.text}")
                for rel in entity.relations[:6]:
                    lines.append(f"- {rel.label or rel.category} -> {rel.target}")
                sub.append("\n".join(lines))
                used.append({"wiki_id": entity.wiki_id,
                             "title": entity.title,
                             "type": entity.type})
            if sub:
                blocks.append(f"# {w.title}\n\n" + "\n\n".join(sub))
        else:
            # _memory_context already relevance-matched entities to the
            # question via the assessor, so keep them as-is (re-filtering by
            # title would wrongly drop e.g. "Sarah Kim" whose SUMMARY matched
            # but whose title does not contain the question words).
            if used_here:
                blocks.append(f"# {w.title}\n\n" + ctx)
                used.extend(used_here)
    if not blocks:
        return "", []
    return "Relevant memory (across all wikis):\n\n" + "\n\n".join(blocks), used


def _content_tokens(text: str) -> set:
    """Small tokenizer for topical filtering; reuses the assessor's."""
    try:
        from app.verify.assessor import _content_tokens as _t
        return _t(text)
    except Exception:
        import re as _re
        return {w for w in _re.findall(r"[a-z0-9']+", (text or "").lower()) if len(w) > 2}


@router.post("/chat", response_model=ChatResponse)
def chat(user_id: str, body: ChatRequest,
         store: EntityGraphStore = Depends(get_graph_store)):
    """Send a conversation and get a reply informed by stored memory."""
    llm = _client()
    if not llm.configured:
        raise HTTPException(
            status_code=503,
            detail="No LLM API key configured. Set DEEPSEEK_API_KEY to enable chat.")

    latest = next((m.content for m in reversed(body.messages) if m.role == "user"), "")
    if not latest.strip():
        raise HTTPException(status_code=422, detail="No user message to reply to.")

    # ---- Deterministic deletion short-circuit ----
    # A bare "delete/forget/remove <name>" is a deliberate destructive
    # request. Handle it WITHOUT the LLM and WITHOUT the new-topic router: the
    # target is found by name across every reachable wiki (longest-title wins),
    # and the delete is staged for confirmation. Routing a delete by topic
    # shares no vocabulary with the wiki holding the name and either stages a
    # no-op against a junk wiki or fabricates a new one -- exactly the "why is
    # this so dumb" behaviour. Only when the name is NOT found do we fall
    # through to the normal (fragile) intent path.
    registry = WikiRegistry(get_storage_backend())
    try:
        from app.intents.retract import is_delete_request
        wants_delete = is_delete_request(latest)
    except Exception:
        wants_delete = False
    if wants_delete:
        hit = _find_entity_by_text(store, registry, user_id, latest)
        if hit is not None:
            wid, eid, title, facts = hit
            from app.intents.semantica_crud import (
                _describe_deletion, _stage_pending_deletion)
            plan = _describe_deletion(store, wid, eid)
            pending_id = _stage_pending_deletion(
                store.backend, user_id, wid, plan, op="delete_entity")
            tool_results = [{
                "status": "needs_confirmation", "pending_id": pending_id,
                "op": "delete_entity", "entity": eid, "plan": plan,
                "wiki": wid,
                "message": f"Confirm delete_entity on {eid}?",
            }]
            reply = (f"I'll forget {title or eid} (that would remove "
                     f"{facts} stored fact{'s' if facts != 1 else ''})."
                     f"\n\nForget is pending your confirmation: {eid}. "
                     "Nothing was deleted yet. \"Yes, delete\" to confirm.")
            return ChatResponse(reply=reply, context_used=[], memory_hit=False,
                                session_id=body.session_id or new_session_id(),
                                wiki_id=wid, wiki_reason="deterministic deletion",
                                new_wiki_proposal=None,
                                tool_results=tool_results)

    # Resolve which graph answers this. Reads may route freely — a bad read
    # gives a worse answer, where a bad write corrupts a graph — but the
    # choice is always reported so it can be corrected.
    wiki_id, wiki_reason = body.wiki_id, ""
    new_wiki_proposal: dict | None = None
    if wiki_id:
        try:
            registry.require(wiki_id, user_id, ROLE_READ)
            wiki_reason = "requested"
        except WikiNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except WikiAccessDenied as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
    elif body.use_memory:
        # Single-wiki deployment: always the user's own home wiki. No topic
        # scoring, no candidate wikis, nothing ever proposed as a separate
        # page -- ensure_home creates it on first use.
        aggregate = False
        home = registry.ensure_home(user_id)
        if home is not None:
            wiki_id, wiki_reason = home.wiki_id, "single wiki: user's own wiki"

    # ---- Pending-deletion confirm/cancel from a follow-up reply ----
    # After the assistant said "Forget is pending your confirmation: X", an
    # affirmative reply ("yes delete", "go ahead") applies the staged deletion
    # and a clear negative ("no keep it", "cancel") abandons it. Resolved
    # directly without spending a model call; returns the outcome immediately.
    try:
        from app.intents.retract import confirmations_resolved
        from app.intents.semantica_crud import confirm_pending_deletion, cancel_pending_deletion
        from app.wikis.registry import ROLE_WRITE as _RW
        pending_out = confirmations_resolved(user_id, latest, store)
    except Exception:
        pending_out = None
    if pending_out is not None:
        pending_id = pending_out.get("pending_id")
        confirm = pending_out.get("confirm")
        out = {}
        try:
            if confirm:
                def _writer(wiki_id: str) -> bool:
                    try:
                        registry.require(wiki_id or user_id, user_id, _RW)
                        return True
                    except (WikiNotFound, WikiAccessDenied):
                        return False
                out = confirm_pending_deletion(store, user_id, _writer, pending_id)
            else:
                out = cancel_pending_deletion(store.backend, user_id, pending_id)
        except Exception as e:
            out = {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
        status = out.get("status", "")
        name = pending_out.get("entity_label") or pending_out.get("entity") or "item"
        if confirm and status == "applied":
            reply_text = f"Done — {name} was deleted from memory."
        elif confirm:
            reply_text = f"Couldn't delete {name}: {status}."
        elif status == "cancelled":
            reply_text = f"Kept {name} — nothing was deleted."
        elif status == "not_found":
            reply_text = "That pending deletion is already resolved or doesn't exist."
        else:
            reply_text = f"No change: {status}."
        return ChatResponse(reply=reply_text, wiki_id=wiki_id or "",
                            wiki_reason=wiki_reason or "pending-resolution",
                            tool_results=[out], new_wiki_proposal=None,
                            context_used=[], memory_hit=False)

    context, used = ("", [])
    if body.use_memory and wiki_id:
        try:
            context, used = _memory_context(store, wiki_id, latest)
        except Exception:
            # Retrieval is an enhancement. A memory failure must not cost the
            # user their reply.
            logger.warning("memory retrieval failed for %s", user_id, exc_info=True)
        # Cross-wiki fallback: the question is asked against a pinned wiki but
        # nothing relevant lives in it (e.g. "whos Anton" while the active wiki
        # is a different project). Memory about a *name* rarely stays in one
        # context -- look everywhere the user can read rather than answer with
        # an empty "I don't have anything". Keep the pinned wiki's local reason
        # only if the fallback finds nothing.
        if not used:
            try:
                fallback_ctx, fallback_used = _aggregate_memory_context(
                    store, user_id, registry, question=latest)
            except Exception:
                logger.warning("cross-wiki fallback retrieval failed for %s",
                               user_id, exc_info=True)
                fallback_ctx, fallback_used = "", []
            if fallback_used:
                context, used = fallback_ctx, fallback_used
                wiki_reason = (wiki_reason or "requested") + "; found in other wikis: "
                wiki_reason += ", ".join(sorted({u["wiki_id"].split("/")[0]
                                                  for u in fallback_used}))
    elif body.use_memory and aggregate:
        # The question is an overview across projects/items. Read across all
        # reachable wikis so it gets a real cross-wiki picture instead of an
        # empty "no memory" reply.
        try:
            context, used = _aggregate_memory_context(store, user_id, registry,
                                                     question=latest)
            if used:
                wiki_reason = "aggregate across " + ", ".join(
                    sorted({u["wiki_id"].split("/")[0] for u in used}))
        except Exception:
            logger.warning("aggregate memory retrieval failed for %s",
                           user_id, exc_info=True)

    # Attached file text, injected alongside retrieved memory.
    attachment_blocks: list[str] = []
    attachment_refs: list[str] = []
    if body.attachments:
        store_files = FileStore(get_storage_backend())
        for ref in body.attachments[:10]:
            try:
                parsed = parse_file_ref(ref)
                if parsed:
                    day, file_id = parsed
                else:
                    file_id = ref
                    day = store_files.find(user_id, file_id)
                if day is None:
                    attachment_blocks.append(f"[Attachment {ref} could not be found.]")
                    continue
                text, meta = store_files.get_text(user_id, file_id, day)
                attachment_refs.append(file_ref(day, file_id))
                if text is None:
                    # Say so rather than passing nothing silently: a model that
                    # is not told a file was unreadable will answer as though
                    # it had read it.
                    attachment_blocks.append(
                        f"[Attachment {meta.name if meta else ref}: "
                        f"{meta.note if meta else 'contents unavailable'}]")
                else:
                    attachment_blocks.append(
                        f"### Attached file: {meta.name}\n\n{text}")
            except FileError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

    parts = [SYSTEM_PROMPT]
    if body.tools_enabled:
        # Teach the model it may issue explicit CRUD intents when the user
        # asks it to remember/forget/change something in memory.
        parts.append(TOOLS_RUBRIC)
    if context:
        parts.append(context)
    if attachment_blocks:
        parts.append("Files the user attached to this message:\n\n"
                     + "\n\n".join(attachment_blocks))
    system = "\n\n".join(parts)
    history = body.messages[-CHAT_HISTORY_TURNS:]
    transcript = "\n\n".join(
        f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" for m in history)

    try:
        reply = llm.complete(system, transcript, json_mode=False,
                             temperature=body.temperature,
                             max_tokens=get_settings().llm_max_tokens)
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

    reply = reply.strip()

    # If tools are enabled, execute any <intents> block the model emitted.
    # NOTE: this runs even when wiki_id is empty (a brand-new topic with only a
    # wiki *proposal*): inline intents must still execute, and the new-topic
    # fallback materialises the proposed wiki so facts land there.
    tool_results: list[dict] | None = None
    if body.tools_enabled:
        from app.intents.executor import WRITE_OPS, parse_intents, execute_intents
        intents = parse_intents(reply)
        # If the model did NOT emit an intent but the user clearly RETRACTED
        # or CORRECTED a memory, structure the claim and let Semantica detect
        # + apply it. Without this, "X is no longer true" / "that was wrong"
        # silently leave memory unchanged.
        if not intents:
            from app.intents.semantica_crud import (
                structure_claim, apply_semantica_conflict)
            from app.intents.retract import is_retraction
            if is_retraction(latest) and wiki_id:
                # A retraction/correction only makes sense against a wiki the
                # text belongs to; on a brand-new topic (wiki_id empty, only a
                # proposal) there is nothing to correct yet, so the fallback
                # below handles it instead.
                try:
                    claims = structure_claim(latest, context, llm, retraction_hint=True)
                except Exception as e:
                    logger.warning("claim structuring failed: %s", e)
                    claims = []
                def _writer_all(wiki_id: str) -> bool:
                    try:
                        registry.require(wiki_id or user_id, user_id, ROLE_WRITE)
                        return True
                    except (WikiNotFound, WikiAccessDenied):
                        return False
                if claims:
                    tool_results = apply_semantica_conflict(
                        store, user_id, claims, _writer_all, wiki_id=wiki_id,
                        confirm_deletes=True)
                    ok = sum(1 for r in tool_results if r["status"] == "applied")
                    # Destructive deletes are staged, not auto-applied. Surface
                    # them so the client can show a confirm prompt.
                    pending = [r for r in tool_results
                               if r.get("status") == "needs_confirmation"]
                    # (Note deliberately NOT appended to the visible reply:
                    # the user does not want to see a "[Memory updated...]"
                    # confirmation anywhere. tool_results carries the counts.)
                    if pending:
                        names = ", ".join(
                            str(p.get("plan", {}).get("entity")) for p in pending)
                        reply += ("\n_Forget is pending confirmation for: " + names +
                                  ". Review and confirm before it deletes._")
            elif new_wiki_proposal:
                # A genuinely new topic was detected (the router/LLM proposed a
                # NEW wiki), but the model replied conversationally without an
                # intents block -- so nothing would be stored. Structure the
                # user's claims, create the entities they reference (they are
                # brand new -- apply_semantica_conflict can only touch existing
                # nodes), and write the facts. Falls back to nothing when the
                # message is a question (structure_claim returns []).
                try:
                    claims = structure_claim(latest, context, llm,
                                             new_topic_hint=True)
                except Exception as e:
                    logger.warning("claim structuring for new topic failed: %s", e)
                    claims = []
                if claims:
                    wid = _materialize_proposal(user_id, registry,
                                                new_wiki_proposal)
                    new_wiki_proposal = None
                    wiki_id = wid
                    wiki_reason = "new topic; materialised " + wid
                    def _writer_new(wiki_id: str) -> bool:
                        try:
                            registry.require(wiki_id or user_id, user_id, ROLE_WRITE)
                            return True
                        except (WikiNotFound, WikiAccessDenied):
                            return False
                    # property-claims -> create_entity + add_fact intents, then
                    # run them through the same execute_intents path as inline
                    # intents so entity creation actually succeeds.
                    from app.intents.semantica_crud import claims_to_intents
                    new_intents = claims_to_intents(claims)
                    for it in new_intents:
                        it.setdefault("wiki", wid)
                    tool_results = execute_intents(store, user_id, new_intents,
                                                   writer_for=_writer_new)
                    ok = sum(1 for r in tool_results
                             if r.get("status") == "applied")
                    # (Confirmation intentionally not appended to the visible
                    # reply; counts live in tool_results.)
            else:
                intents = []
        if intents:
            registry_inst = registry  # already built above
            # A brand-new topic carries a wiki *proposal* (the router never
            # materialises it) but inline intents write NOW. Without creating
            # the wiki first, every write targets a non-existent wiki and is
            # silently skipped. Create it from the proposal and route the
            # intents there.
            write_target = wiki_id
            if not write_target and new_wiki_proposal and any(
                    i.get("op") in WRITE_OPS for i in intents):
                write_target = _materialize_proposal(user_id, registry_inst,
                                                     new_wiki_proposal)
                new_wiki_proposal = None  # materialised; no longer a pending proposal
                wiki_id = write_target
                wiki_reason = "new topic; materialised " + write_target
            def _writer(wiki_id: str) -> bool:
                try:
                    registry_inst.require(wiki_id or user_id, user_id, ROLE_WRITE)
                    return True
                except (WikiNotFound, WikiAccessDenied):
                    return False
            for it in intents:
                it.setdefault("wiki", write_target or wiki_id or user_id)
            # Split destructive intents (delete_entity/delete_fact) out of the
            # batch: they are staged and require explicit user confirmation,
            # never auto-applied. Everything else executes immediately.
            destructive = [i for i in intents
                           if i.get("op") in ("delete_entity", "delete_fact")]
            safe = [i for i in intents if i not in destructive]
            tool_results = []
            if safe:
                tool_results += execute_intents(store, user_id, safe,
                                                writer_for=_writer)
            if destructive:
                from app.intents.semantica_crud import (
                    _describe_deletion, _stage_pending_deletion)
                for it in destructive:
                    ent = it.get("entity") or ""
                    # Locate where the entity actually lives rather than
                    # trusting the topic-routed wiki: a pure delete request
                    # ('delete sam whitfield') shares no topical words with
                    # the wiki holding Sam, so routing alone often targets
                    # the wrong (or a junk) wiki and stages a no-op delete.
                    located = _locate_entity_wiki(store, registry_inst,
                                                  user_id, ent)
                    wid = located or it.get("wiki") or wiki_id or user_id
                    plan = _describe_deletion(store, wid, ent)
                    pending_id = _stage_pending_deletion(
                        store.backend, user_id, wid, plan,
                        op=it.get("op"),
                        fact_id=it.get("fact_id"))
                    tool_results.append({
                        "status": "needs_confirmation",
                        "pending_id": pending_id,
                        "op": it.get("op"),
                        "entity": ent,
                        "plan": plan,
                        "wiki": wid,
                        "message": f"Confirm {it.get('op')} on {ent}?",
                    })
            # ok = ...  # (kept for reference; no visible confirmation
            # is appended -- tool_results carries the applied counts.)
            for _r in tool_results:
                pass
            # Surface pending deletions in the reply so the client knows to
            # show a confirm prompt. Only names + a hint -- no execution.
            pending = [r for r in tool_results
                       if r.get("status") == "needs_confirmation"]
            if pending:
                names = ", ".join(
                    str(p.get("entity") or p.get("plan", {}).get("entity"))
                    for p in pending)
                if not reply.endswith("."):
                    pass
                reply += ("\nForget is pending your confirmation: " + names +
                          ". Nothing was deleted yet. \"Yes, delete\" to confirm.")

    session_id = body.session_id or new_session_id()
    if body.log:
        # Persist BOTH turns. Without this the conversation exists only in the
        # browser, so closing the tab loses it — and re-extraction with an
        # improved prompt becomes impossible because the source is gone.
        try:
            # The journal records the turn verbatim AND what actually shaped
            # it: which memories were retrieved, which files were attached,
            # which model answered. Without that, a surprising reply cannot be
            # explained after the fact — the messages alone do not show what
            # the model was given.
            SessionLog(get_storage_backend()).append(user_id, session_id, [
                {"role": "user", "content": latest,
                 "attachments": attachment_refs or None},
                {"role": "assistant", "content": reply,
                 "model": getattr(llm, "model", None),
                 "memory_used": [c["wiki_id"] for c in used] or None,
                 "attachments_used": attachment_refs or None},
            ])
        except SessionIdError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            # Logging must not cost the user their reply.
            logger.warning("could not log session %s for %s", session_id, user_id,
                           exc_info=True)

    return ChatResponse(reply=strip_intents(reply),
                        context_used=used, memory_hit=bool(used),
                        session_id=session_id if body.log else None,
                        wiki_id=wiki_id, wiki_reason=wiki_reason,
                        new_wiki_proposal=new_wiki_proposal,
                        tool_results=tool_results)


def _find_entity_by_text(store: EntityGraphStore, registry: WikiRegistry,
                         user_id: str, text: str):
    """Cross-wiki name lookup for a bare delete request.

    Returns (wiki_id, entity_id, entity_title, node_count, facts) for the
    most specific reachable entity whose title/alias appears in `text`, else
    None. Longest title wins on ties. This is the deterministic target for
    'delete/forget/remove <name>' so it NEVER rides the new-topic router and
    NEVER fabricates a junk wiki."""
    import re as _re
    try:
        reachable = list(registry.list_for(user_id, role=ROLE_READ))
    except Exception:
        reachable = []
    low = text.lower()
    best = None
    best_len = 0
    for wiki in reachable:
        wid = getattr(wiki, "wiki_id", None) or getattr(wiki, "id", None)
        if not wid:
            continue
        try:
            for eid in store.list_entities(wid):
                ent = store.get_entity(wid, eid, touch=False)
                if ent is None:
                    continue
                names = [ent.title] + list(getattr(ent, "aliases", None) or [])
                for n in names:
                    nn = (n or "").strip()
                    if not nn or len(nn) < 3:
                        continue
                    if nn.lower() in low and len(nn) > best_len:
                        best = (wid, eid, ent.title,
                                len(getattr(ent, "facts", None) or []))
                        best_len = len(nn)
        except Exception:
            continue
    if best:
        wid, eid, title, facts = best
        return wid, eid, title, facts
    return None


def _locate_entity_wiki(store: EntityGraphStore, registry: WikiRegistry,
                         user_id: str, entity: str) -> str | None:
    """Find which reachable wiki actually stores `entity` (e.g. a node id
    'person/sam-whitfield', or a bare title).

    Deletions/retractions must act on the wiki the entity LIVES in, not on
    the topic-routed wiki for the sentence -- 'delete sam whitfield' shares
    no topical vocabulary with the wiki that holds Sam, so routing alone
    sends the delete to the wrong (or a junk) wiki. Searches every wiki the
    caller can read via the entity cache (cheap after the first fetch).
    Returns the wiki_id that has the entity, or None if not found anywhere.
    """
    if not entity:
        return None
    entity = entity.strip()
    try:
        reachable = list(registry.list_for(user_id, role=ROLE_READ))
    except Exception:
        reachable = []
    # Exact node-id match first (fastest, most authoritative).
    for wiki in reachable:
        wid = getattr(wiki, "wiki_id", None) or getattr(wiki, "id", None)
        if not wid:
            continue
        try:
            if store.get_entity(wid, entity, touch=False) is not None:
                return wid
        except Exception:
            continue
    # Fall back to a title match (the LLM sometimes emits a bare name).
    for wiki in reachable:
        wid = getattr(wiki, "wiki_id", None) or getattr(wiki, "id", None)
        if not wid:
            continue
        try:
            for e in store.list_entities(wid):
                if (getattr(e, "wiki_id", "") == entity
                        or (getattr(e, "title", "") or "").strip().lower()
                           == entity.lower()):
                    return wid
        except Exception:
            continue
    return None


def _materialize_proposal(user_id: str, registry: WikiRegistry,
                         proposal: dict) -> str:
    """Create the wiki a 'new topic' chat turn is about to write into.

    Chat inline intents (tools_enabled) write immediately, but the router only
    *proposes* a new wiki for a genuinely new topic -- it never materialises it.
    Without this, entities for a brand-new topic are written to a non-existent
    wiki and all intents are silently SKIPPED ('no write access'). This creates
    the proposed wiki (reusing an existing slug when it is active, echoing
    routes_extract._materialize_new_wiki) and returns its wiki_id so the
    intent writes can target it.
    """
    title = (proposal.get("title") or "").strip() \
        or (proposal.get("provisional_title") or "").strip() or "New Wiki"
    slug = slugify_wiki(title) or "new-wiki"
    try:
        registry.create(title, created_by=user_id,
                        description=proposal.get("description") or "",
                        wiki_id=slug,
                        topic=proposal.get("topic") or "",
                        tags=proposal.get("tags") or None)
        return slug
    except WikiError:
        existing = registry.get(slug)
        if existing is not None and not existing.archived:
            return existing.wiki_id
        disamb = slug
        n = 2
        while registry.get(disamb) is not None:
            disamb = f"{slug}-{n}"
            n += 1
        registry.create(title, created_by=user_id,
                        description=proposal.get("description") or "",
                        wiki_id=disamb,
                        topic=proposal.get("topic") or "",
                        tags=proposal.get("tags") or None,
                        allow_similar=True)
        return disamb


def _propose_new_wiki(registry: WikiRegistry, llm: LLMClient, user_id: str,
                      text: str, store=None) -> tuple[dict | None, str | None]:
    """Ask the LLM whether `text` is a genuinely new topic.

    Returns (proposal, belongs_to): a non-None proposal means the LLM wants a
    NEW wiki (title/description/topic/reason); belongs_to, when proposal is
    None, is the existing wiki the text should route to. Both None when the
    LLM is unavailable or uncertain.

    NEW-WIKI CONTENT GATE: a wiki must only ever be created for DURABLE DATA.
    Meta-chatter ("memory clean up", "yes delete it", "thanks", a bare
    question) describes an action or a wish, not facts to remember -- it must
    NEVER spawn a wiki. We run the cheap assessor first; if the message is not
    store/review-worthy we refuse to even ask the LLM. This is what stops
    junk pages like 'memory-cleanup'.
    """
    if store is not None:
        try:
            _a = store.assessor if hasattr(store, "assessor") else None
            if _a is None:
                from app.verify.assessor import ConversationAssessor
                _a = ConversationAssessor(store)
            _gate = _a.assess(user_id, text)
            _allowed = {"store", "review"}
            if _gate.decision not in _allowed:
                return None, None
        except Exception:
            pass  # gate is best-effort; never block on it
    else:
        # No store available (rare): refuse to propose unless the text is
        # clearly long enough to hold a durable claim.
        if len((text or "").split()) < 8:
            return None, None
    reachable = registry.list_for(user_id, role=ROLE_READ)
    verdict = llm.evaluate_new_topic(
        text, [{"wiki_id": w.wiki_id, "title": w.title, "topic": w.topic}
               for w in reachable])
    if not verdict.get("is_new_topic"):
        return None, verdict.get("belongs_to") or None
    return ({"title": (verdict.get("title") or "").strip() or "New Wiki",
             "description": verdict.get("description") or "",
             "topic": verdict.get("topic") or "",
             "reason": verdict.get("reason") or "new topic"}, None)
