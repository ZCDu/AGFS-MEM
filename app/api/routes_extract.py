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
from app.wikis.registry import (ROLE_WRITE, WikiAccessDenied, WikiNameCollision,
                                WikiNotFound, WikiRegistry)
from app.wikis.router import WikiRouter
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


VALID_OPS = {"upsert_entity", "add_fact", "link_entities"}
REQUIRED_FIELDS = {
    "upsert_entity": ("type", "title"),
    "add_fact": ("text",),
    "link_entities": ("target_wiki_id",),
}


def _resolve_target(user_id: str, requested: str | None, text: str,
                    store) -> tuple[str, str]:
    """Which graph a write goes to. Returns (wiki_id, reason).

    Unlike a read, this refuses to guess. An ambiguous or absent match raises
    409 with the candidates rather than picking: a read sent to the wrong
    graph gives a worse answer, but a write sent to the wrong graph corrupts
    it, and nothing downstream detects that.
    """
    registry = WikiRegistry(get_storage_backend())
    if requested:
        try:
            registry.require(requested, user_id, ROLE_WRITE)
        except WikiNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except WikiAccessDenied as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        return requested, "requested"

    decision = WikiRouter(registry, store).route(
        user_id, text, role=ROLE_WRITE, allow_create=True)
    if decision.action == "use" and decision.wiki_id:
        return decision.wiki_id, decision.reason

    # First use: no wiki exists yet, so there is nothing to route to. Every
    # user owns a personal wiki (their own name); the very first conversation
    # lands there rather than being blocked, before any topic wiki exists to
    # join.
    if not registry.list_for(user_id, role=ROLE_WRITE):
        personal = registry.ensure_personal(user_id, user_id)
        if personal is not None:
            return personal.wiki_id, "personal wiki (first use)"

    # A clear "nothing matched" decision means this is a genuinely new topic:
    # create the wiki rather than refusing. Auto-creation still can't
    # fragment the graph -- create_from_decision refuses near-duplicate names
    # and reuses the existing wiki instead.
    if decision.action == "create":
        try:
            wiki = WikiRouter(registry, store).create_from_decision(
                user_id, decision)
        except WikiNameCollision as e:
            return e.existing_id, (
                f"proposed {decision.proposed_title!r} was too close to "
                f"{e.existing_title!r}; reused it")
        return wiki.wiki_id, f"created {wiki.title!r} (new topic)"

    # Only a tie between two existing wikis is genuinely undecidable without
    # corrupting a graph -- that still asks, with the candidates in hand.
    raise HTTPException(status_code=409, detail={
        "message": "Could not decide which wiki to write to.",
        "action": decision.action,
        "reason": decision.reason,
        "proposed_title": decision.proposed_title,
        "candidates": [c.to_dict() for c in decision.candidates],
        "hint": "Send target_wiki explicitly to pick among the candidates."})


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
        target, target_reason = _resolve_target(
            user_id, body.target_wiki, body.text, extractor.store)
        plan = extractor.plan(target, body.text, force=body.force, evidence=evidence)
    except LLMNotConfigured as e:
        # A deployment state, not a fault.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        # The upstream model failed. 502 rather than 500: this service is fine,
        # its dependency is not, and the distinction matters when debugging.
        raise HTTPException(status_code=502, detail=str(e)) from e

    if apply and plan.operations:
        plan = extractor.apply(target, plan)
    out = plan.to_dict()
    out["target_wiki"] = target
    out["target_wiki_reason"] = target_reason
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
    except WikiNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except WikiAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    plan = ExtractionPlan(decision="manual", operations=ops)
    plan = extractor.apply(body.target_wiki, plan)
    applied = sum(1 for o in plan.operations if o.status == "applied")
    failed = [o.to_dict() for o in plan.operations if o.status == "failed"]
    return {"applied": applied, "failed": len(failed),
            "failures": failed, "operations": [o.to_dict() for o in plan.operations]}
