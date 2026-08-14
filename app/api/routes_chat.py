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
from app.verify.assessor import ConversationAssessor
from app.wikis.registry import ROLE_READ, WikiAccessDenied, WikiNotFound, WikiRegistry
from app.wikis.router import WikiRouter

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
    temperature: float = Field(0.7, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    reply: str
    context_used: list[dict]
    memory_hit: bool
    session_id: str | None = None
    wiki_id: str | None = None
    wiki_reason: str = ""


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

    for link in assessment.related[:MAX_CONTEXT_ENTITIES]:
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

    # Resolve which graph answers this. Reads may route freely — a bad read
    # gives a worse answer, where a bad write corrupts a graph — but the
    # choice is always reported so it can be corrected.
    registry = WikiRegistry(get_storage_backend())
    wiki_id, wiki_reason = body.wiki_id, ""
    if wiki_id:
        try:
            registry.require(wiki_id, user_id, ROLE_READ)
            wiki_reason = "requested"
        except WikiNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except WikiAccessDenied as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
    elif body.use_memory:
        decision = WikiRouter(registry, store).route(user_id, latest)
        if decision.action == "use":
            wiki_id, wiki_reason = decision.wiki_id, decision.reason
        else:
            # Ambiguous or no match: answer without memory rather than guess.
            # A wrong graph is worse than no graph, because the model cannot
            # tell that what it was given is unrelated.
            wiki_reason = decision.reason

    context, used = ("", [])
    if body.use_memory and wiki_id:
        try:
            context, used = _memory_context(store, wiki_id, latest)
        except Exception:
            # Retrieval is an enhancement. A memory failure must not cost the
            # user their reply.
            logger.warning("memory retrieval failed for %s", user_id, exc_info=True)

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
                             temperature=body.temperature, max_tokens=1200)
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

    reply = reply.strip()

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

    return ChatResponse(reply=reply, context_used=used, memory_hit=bool(used),
                        session_id=session_id if body.log else None,
                        wiki_id=wiki_id, wiki_reason=wiki_reason)
