"""
Conversation session endpoints.

Retrieval by identity, not by content. "Show me that conversation" is one GET;
"when did we discuss X" belongs to the graph, which answers it from an index
already in memory rather than by scanning history that grows without bound.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_user
from app.deps import get_storage_backend
from app.rawlog.sessions import SessionIdError, SessionLog, new_session_id

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}/sessions", tags=["sessions"])


def _log() -> SessionLog:
    return SessionLog(get_storage_backend())


class SessionMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str
    ts: str | None = None


class AppendRequest(BaseModel):
    session_id: str | None = Field(
        None, description="Omit to start a new session; the id is returned.")
    messages: list[SessionMessage] = Field(..., min_length=1)


@router.post("")
def append_messages(user_id: str, body: AppendRequest):
    """Append to a session, creating it if no id is given."""
    log = _log()
    session_id = body.session_id or new_session_id()
    try:
        return log.append(user_id, session_id,
                          [m.model_dump(exclude_none=True) for m in body.messages])
    except SessionIdError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("")
def list_sessions(
    user_id: str,
    on: date | None = Query(None, description="Single day, YYYY-MM-DD"),
    start: date | None = Query(None),
    end: date | None = Query(None),
):
    """Session summaries, by day or over a range. Defaults to today."""
    log = _log()
    if on is not None:
        return log.list_day(user_id, on)
    if start is not None and end is not None:
        if end < start:
            raise HTTPException(status_code=422, detail="end is before start")
        return log.list_range(user_id, start, end)
    return log.list_day(user_id, datetime.now(timezone.utc).date())


@router.get("/{session_id}")
def read_session(
    user_id: str, session_id: str,
    on: date | None = Query(None, description="The session's date. Without it "
                                              "the session must be searched for."),
    as_transcript: bool = Query(False, description="Return the rendered "
                                                   "transcript instead of records"),
):
    """One session. Pass `on` to make it a single storage read."""
    log = _log()
    try:
        if as_transcript:
            text = log.transcript(user_id, session_id, day=on)
            if not text:
                raise HTTPException(status_code=404, detail="No such session.")
            return {"session_id": session_id, "transcript": text}
        records = log.read(user_id, session_id, day=on)
    except SessionIdError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not records:
        raise HTTPException(status_code=404, detail="No such session.")
    return {"session_id": session_id, "messages": records}
