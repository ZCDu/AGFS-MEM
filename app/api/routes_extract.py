"""
LLM extraction endpoints.

    POST /extract          assess, extract, return a PLAN. Writes nothing.
    POST /extract?apply=true   the same, then execute it.
    POST /extract/apply    execute a plan you have reviewed or edited.

The default is to write nothing. A model that writes straight into long-term
memory can corrupt it in ways nobody notices — a wrong fact on the right
entity is indistinguishable from a right one at a glance — so the reviewable
plan is the default and applying is a deliberate second act.
"""

from __future__ import annotations

from datetime import datetime, timezone

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_user
from app.rawlog.sessions import evidence_ref
from app.wikis.registry import (ROLE_WRITE, WikiAccessDenied, WikiError,
                                WikiNotFound, WikiRegistry, slugify_wiki)
from app.deps import get_extractor, get_storage_backend
from app.extract.extractor import ConversationExtractor, ExtractionPlan, Operation
from app.extract.llm import LLMError, LLMNotConfigured

logger = logging.getLogger("memory_backend.extract")

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}", tags=["extract"])


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1,
                      description="Raw conversation text to extract memory from")
    force: bool = Field(False,
                        description="Skip the assessor gate and call the model "
                                    "regardless. Costs a request on text the "
                                    "cheap filter already rejected.")
    target_wiki: str | None = Field(
        None, description="Which graph to write into. Omitted, the router "
                          "proposes one and returns the decision WITHOUT "
                          "applying — a wrong write corrupts a graph and "
                          "nothing downstream detects it.")
    session_id: str | None = Field(
        None, description="Session this text came from. Recorded on every "
                          "extracted fact as evidence, so the stored claim can "
                          "be traced back to the conversation that produced it.")
    session_date: str | None = Field(
        None, description="The session's date (YYYY-MM-DD). Included in the "
                          "evidence reference so resolving it is one read "
                          "rather than a search back through daily listings.")


class OperationIn(BaseModel):
    op: str
    wiki_id: str
    payload: dict
    reason: str = ""
    status: str = "proposed"
    detail: str = ""


class ApplyRequest(BaseModel):
    target_wiki: str = Field(
        ..., description="Graph to write into. Required here: a reviewed plan "
                         "carries no text to route on, and guessing would "
                         "write someone's memory into the wrong graph.")
    operations: list[OperationIn] = Field(
        ..., description="Operations from a plan. Drop or edit any you disagree "
                         "with before submitting.")
    new_wiki_proposal: dict | None = Field(
        None, description="When the plan proposed creating a NEW wiki (a "
                          "genuinely new topic), echo back its proposal so it "
                          "can be created on apply. Omit when target_wiki "
                          "already exists.")
    session_id: str | None = Field(
        None, description="Session this reviewed plan came from. When present, "
                          "the session's messages are marked examined after a "
                          "successful apply, so the auto-capture timer does not "
                          "re-examine what a human just reviewed and approved.")
    session_date: str | None = Field(
        None, description="The session's date (YYYY-MM-DD), mirroring /extract.")


VALID_OPS = {"upsert_entity", "add_fact", "link_entities",
              "delete_entity", "delete_fact"}
REQUIRED_FIELDS = {
    "upsert_entity": ("type", "title"),
    "add_fact": ("text",),
    "link_entities": ("target_wiki_id",),
    "delete_entity": ("entity",),
    "delete_fact": ("entity", "fact_id"),
}


def _resolve_target(user_id: str, requested: str | None, text: str,
                    store) -> tuple[str, str, dict | None]:
    """Which graph a write goes to. Returns (target_wiki, reason, pending).

    Single-wiki deployment: every user's durable content lands in their own
    home wiki (wiki_id == user_id), created on first use if it doesn't exist
    yet. There is no topic-based routing to other wikis and nothing is ever
    proposed as a new wiki -- distinct subjects show up as separate clusters
    within the one graph (see the GUI's cluster view), not as separate pages
    the user has no way to find without a wiki switcher.

    `pending` is always None here: nothing needs to be materialised later, the
    home wiki is guaranteed to exist by the time this returns.
    """
    registry = WikiRegistry(get_storage_backend())
    if requested:
        try:
            registry.require(requested, user_id, ROLE_WRITE)
        except WikiNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except WikiAccessDenied as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        return requested, "requested", None

    # ---- retraction / deletion short-circuit (BEFORE router) ----
    # "remove the fact that Pamela is Black", "forget Sam Whitfield",
    # "Pamela is not Black, she is Latino" must act on the wiki that ALREADY
    # stores the subject, not on a topic-routed (possibly brand-new) wiki.
    # Running the router first for a retraction sends it to an empty new wiki
    # (nothing matches there), so _plan_removal finds no subject and we fall
    # through to the create path => a junk pending wiki + the fact is never
    # fixed. Detect the intent up front and lock the target to the existing
    # wiki holding the subject (mirrors the chat deterministic-delete path).
    from app.intents.retract import is_retraction, is_delete_request
    from app.api.routes_chat import _find_entity_by_text
    if is_retraction(text) or is_delete_request(text):
        hit = None
        try:
            hit = _find_entity_by_text(store, registry, user_id, text)
        except Exception:
            hit = None
        if hit is not None:
            wid = hit[0]
            try:
                registry.require(wid, user_id, ROLE_WRITE)
                return wid, "deterministic retraction/delete target", None
            except (WikiNotFound, WikiAccessDenied):
                pass  # not writable -> fall through to normal routing

    # Single-wiki deployment: everything durable goes into the user's own
    # home wiki. No topic scoring, no candidate wikis, nothing proposed as a
    # separate page -- ensure_home creates it on first use and returns the
    # same wiki every time after that.
    home = registry.ensure_home(user_id)
    if home is None:
        raise HTTPException(status_code=500,
                            detail=f"Could not create the home wiki for {user_id!r}.")
    return home.wiki_id, "single wiki: user's own wiki", None


@router.post("/extract")
def extract(
    user_id: str,
    body: ExtractRequest,
    apply: bool = Query(False, description="Execute the plan immediately"),
    extractor: ConversationExtractor = Depends(get_extractor),
):
    """Assess the conversation, and if it is worth keeping, extract entities,
    facts and relations from it.

    The assessor runs first and gates the model call, so ordinary chatter
    costs nothing. `llm_used: false` in the response means it never reached
    the model, and `assessment` says why.
    """
    try:
        evidence = None
        if body.session_id:
            # Date included so resolving the pointer is a single read; without
            # it the session has to be located by walking backwards through
            # daily listings.
            day = body.session_date or datetime.now(timezone.utc).date().isoformat()
            evidence = evidence_ref(day, body.session_id)
        target, target_reason, pending = _resolve_target(
            user_id, body.target_wiki, body.text, extractor.store)
        plan = extractor.plan(target, body.text, force=body.force,
                              evidence=evidence, pending_new_wiki=pending)
    except LLMNotConfigured as e:
        # A deployment state, not a fault.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        # The upstream model failed. 502 rather than 500: this service is fine,
        # its dependency is not, and the distinction matters when debugging.
        raise HTTPException(status_code=502, detail=str(e)) from e

    # The plan may have finalised a better (LLM-named) wiki id for a new
    # topic; prefer it over the provisional one from routing.
    if plan.target_wiki:
        target = plan.target_wiki

    if apply and plan.operations:
        # Materialise the LLM-named new wiki only if the plan actually kept a
        # NEW-wiki proposal. When the plan's topic judgment concluded this is
        # a CONTINUATION of an existing topic, `_name_new_wiki` dropped the
        # proposal and re-pointed target at the existing wiki -- in that case
        # there is nothing new to create (materialising the stale provisional
        # pending dict with no LLM title would only crash on a slug
        # collision).
        if pending is not None and plan.new_wiki_proposal is not None:
            _materialize_new_wiki(user_id, plan.new_wiki_proposal)
        plan = extractor.apply(target, plan)
        # OPTION A IDENTITY: lock the wiki's identity to its first entities.
        try:
            reg2 = WikiRegistry(get_storage_backend())
            wiki = reg2.get(target)
            if wiki is not None and not wiki.identity_set:
                reg2.set_identity(target, extractor.store.list_entities(target))
            _ensure_wiki_tags(reg2, target)
        except Exception:
            logger.debug("could not set identity for %s", target, exc_info=True)
        # A human reviewed and applied this conversation: mark its session
        # examined so the auto-capture timer never re-examines what was just
        # deliberately approved. This is what keeps the timer from
        # contaminating the reviewed data with a second, uncontested write.
        _mark_session_examined(user_id, body)
    out = plan.to_dict()
    out["target_wiki"] = target
    out["target_wiki_reason"] = target_reason
    if plan.new_wiki_proposal is not None:
        out["new_wiki_proposal"] = plan.new_wiki_proposal
    return out


@router.post("/extract/apply")
def apply_plan(
    user_id: str,
    body: ApplyRequest,
    extractor: ConversationExtractor = Depends(get_extractor),
):
    """Execute a reviewed plan.

    Operations arrive from a client, so they are re-validated here rather than
    trusted — the plan may have been edited, and this endpoint is reachable
    without ever calling /extract.
    """
    ops: list[Operation] = []
    for raw in body.operations:
        if raw.op not in VALID_OPS:
            raise HTTPException(status_code=422,
                                detail=f"Unknown operation {raw.op!r}; "
                                       f"expected one of {sorted(VALID_OPS)}")
        missing = [f for f in REQUIRED_FIELDS[raw.op] if not raw.payload.get(f)]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"{raw.op} for {raw.wiki_id!r} is missing {missing}")
        if not raw.wiki_id:
            raise HTTPException(status_code=422, detail="wiki_id is required")
        ops.append(Operation(op=raw.op, wiki_id=raw.wiki_id, payload=raw.payload,
                             reason=raw.reason))

    registry = WikiRegistry(get_storage_backend())
    try:
        registry.require(body.target_wiki, user_id, ROLE_WRITE)
    except WikiNotFound:
        # The target doesn't exist yet. That's expected only when the plan
        # proposed a NEW wiki (a genuinely new topic) that hasn't been
        # materialised; without a proposal this is an error.
        if not body.new_wiki_proposal:
            raise HTTPException(status_code=404,
                                detail=f"No wiki {body.target_wiki!r}.") from None
        _materialize_new_wiki(user_id, body.new_wiki_proposal)
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    plan = ExtractionPlan(decision="manual", operations=ops)
    plan = extractor.apply(body.target_wiki, plan)
    applied = sum(1 for o in plan.operations if o.status == "applied")
    _refresh_wiki_stats(registry, body.target_wiki)
    _ensure_wiki_tags(registry, body.target_wiki)
    failed = [o.to_dict() for o in plan.operations if o.status == "failed"]
    # A human reviewed and applied this plan: if it names its source session,
    # mark that session examined so the timer never re-examines it.
    if applied:
        _mark_session_examined(user_id, body)
    return {"applied": applied, "failed": len(failed),
            "failures": failed, "operations": [o.to_dict() for o in plan.operations]}


def _mark_session_examined(user_id: str, body) -> None:
    """Best-effort: mark the session a reviewed-and-applied plan came from as
    examined, so the auto-capture timer does not re-examine (and re-write)
    what a human already reviewed and approved. No-op when the request does not
    name a session, or when the session cannot be found. Never raises -- the
    apply already succeeded and must not fail afterwards over bookkeeping.
    """
    sid = getattr(body, "session_id", None)
    if not sid:
        return
    try:
        from app.rawlog.sessions import SessionLog
        from app.deps import get_storage_backend
        from datetime import date
        log = SessionLog(get_storage_backend())
        day = None
        raw_day = getattr(body, "session_date", None)
        if raw_day:
            try:
                day = date.fromisoformat(str(raw_day))
            except ValueError:
                day = None
        unexamined = log.read_unexamined(user_id, sid, day=day)
        if unexamined:
            log.mark_examined(user_id, sid, [i for i, _ in unexamined],
                              day=day, by="interactive-review")
    except Exception:
        logger.debug("could not mark session %r examined", sid, exc_info=True)


def _ensure_wiki_tags(registry, wiki_id: str) -> None:
    """Backfill curated tags for a wiki that has none yet, derived from its
    topic + title content words. Best-effort and idempotent: wikis that
    already have tags (e.g. LLM-tagged at creation) are left alone. This is
    the (b) half of the tag system: even a wiki that entered through a path
    that did not tag it gets usable tags on its first write, so the router's
    TAG GATE has labels to match against.
    """
    try:
        wiki = registry.get(wiki_id)
        if wiki is None or wiki.tags:
            return
        from app.verify.assessor import _content_tokens
        tags = sorted(_content_tokens(f"{wiki.title} {wiki.topic}"))[:8]
        if tags:
            registry.set_tags(wiki_id, tags)
    except Exception:
        logger.debug("could not ensure tags for %s", wiki_id, exc_info=True)


def _refresh_wiki_stats(registry, wiki_id: str) -> None:
    """Keep the registry's cached entity_count/sample_entities current after
    an apply writes entities. Best-effort: a stale hint only routes/summarises
    worse; it must never fail an already-succeeded write.
    """
    try:
        store = get_extractor().store
        ids = store.list_entities(wiki_id)
        registry.refresh_stats(
            wiki_id, len(ids),
            [e.title for e in store.manifest.list_entries(wiki_id)[:25]])
    except Exception:
        logger.debug("could not refresh wiki stats for %s", wiki_id,
                     exc_info=True)


def _materialize_new_wiki(user_id: str, proposal: dict) -> None:
    """Create a wiki that a plan proposed for a genuinely new topic.

    Called only during apply, so a plan you discard leaves no trace. Uses the
    LLM-proposed title/description/topic; a near-duplicate name reuses the
    existing wiki rather than splitting the graph.
    """
    registry = WikiRegistry(get_storage_backend())
    title = (proposal.get("title") or "").strip() \
        or (proposal.get("provisional_title") or "").strip() \
        or "New Wiki"
    slug = slugify_wiki(title)
    try:
        registry.create(title, created_by=user_id,
                        description=proposal.get("description") or "",
                        wiki_id=slug,
                        topic=proposal.get("topic") or "",
                        tags=proposal.get("tags") or None)
        logger.info("materialised new wiki %r from applied plan", title)
        return registry.get(slug)
    except WikiError:
        # The slug already names a wiki. Reuse it ONLY if it is active (same
        # topic -> merging is correct). An archived wiki must never receive new
        # content, so disambiguate into a distinct new wiki instead.
        existing = registry.get(slug)
        if existing is not None and not existing.archived:
            logger.info("proposed wiki %r already exists (active); reusing it",
                        title)
            return existing
        disamb = slug
        n = 2
        while registry.get(disamb) is not None:
            disamb = f"{slug}-{n}"
            n += 1
        logger.info("proposed wiki %r is archived/blocked; creating %r",
                    slug, disamb)
        created = registry.create(title, created_by=user_id,
                                  description=proposal.get("description") or "",
                                  wiki_id=disamb,
                                  topic=proposal.get("topic") or "",
                                  tags=proposal.get("tags") or None,
                                  allow_similar=True)
        return created
