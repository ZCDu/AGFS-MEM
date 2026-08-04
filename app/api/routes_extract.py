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
from app.deps import get_extractor
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
    operations: list[OperationIn] = Field(
        ..., description="Operations from a plan. Drop or edit any you disagree "
                         "with before submitting.")


VALID_OPS = {"upsert_entity", "add_fact", "link_entities"}
REQUIRED_FIELDS = {
    "upsert_entity": ("type", "title"),
    "add_fact": ("text",),
    "link_entities": ("target_wiki_id",),
}


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
        plan = extractor.plan(user_id, body.text, force=body.force, evidence=evidence)
    except LLMNotConfigured as e:
        # A deployment state, not a fault.
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LLMError as e:
        # The upstream model failed. 502 rather than 500: this service is fine,
        # its dependency is not, and the distinction matters when debugging.
        raise HTTPException(status_code=502, detail=str(e)) from e

    if apply and plan.operations:
        plan = extractor.apply(user_id, plan)
    return plan.to_dict()


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

    plan = ExtractionPlan(decision="manual", operations=ops)
    plan = extractor.apply(user_id, plan)
    applied = sum(1 for o in plan.operations if o.status == "applied")
    failed = [o.to_dict() for o in plan.operations if o.status == "failed"]
    return {"applied": applied, "failed": len(failed),
            "failures": failed, "operations": [o.to_dict() for o in plan.operations]}
