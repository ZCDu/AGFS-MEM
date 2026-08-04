"""
Assessment endpoint — PLAN.md §4 steps 1-2, and the decision-support half of
§12's Golden Rule 2 ("your job: make LTM accessible and provide decision
signals; the agent's job: decide, using those signals").

Deliberately read-only. It reports what it would store and why; the caller
decides whether to act. That keeps it cheap enough to run on every turn,
which is the entire point of a first-stage filter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Depends
from pydantic import BaseModel, Field

from app.auth import require_user
from app.deps import get_graph_store
from app.graph.store import EntityGraphStore
from app.verify.assessor import ConversationAssessor

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}", tags=["verify"])


class AssessRequest(BaseModel):
    text: str = Field(..., description="Raw conversation text to assess")
    min_words: int | None = Field(None, description="Override the length gate")
    store_threshold: float | None = None
    review_threshold: float | None = None
    redundancy_threshold: float | None = Field(
        None, description="Coverage above which text counts as already known (PLAN §4 uses 0.8)")


@router.post("/assess")
def assess_conversation(
    user_id: str,
    body: AssessRequest,
    store: EntityGraphStore = Depends(get_graph_store),
):
    """Score raw text for relevance, importance, novelty and density, link it
    to existing entities, and recommend store / review / skip.

    No LLM, no embeddings, no network. Costs one manifest read, normally
    served from the in-memory cache.
    """
    kwargs = {k: v for k, v in {
        "min_words": body.min_words,
        "store_threshold": body.store_threshold,
        "review_threshold": body.review_threshold,
        "redundancy_threshold": body.redundancy_threshold,
    }.items() if v is not None}
    return ConversationAssessor(store, **kwargs).assess(user_id, body.text).to_dict()
