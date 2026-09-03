"""Medium-term memory notes ("stickers").

CRUD + tag query + expiry surfacing for the middle memory tier. Notes are cheap
to write (one PUT, no LLM), live by their tags, and can expire. The expire
status endpoint is what a periodic sweep (or the UI) uses to re-surface notes
that are now due, so a follow-up is not silently forgotten.

    POST   /v1/users/{u}/notes            create a note (text, tags, expires_at)
    GET    /v1/users/{u}/notes?tag=&active=   list notes
    GET    /v1/users/{u}/notes/expired   active/expired counts + expired list
    GET    /v1/users/{u}/notes/{id}      fetch one
    DELETE /v1/users/{u}/notes/{id}      delete one
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_user
from app.deps import get_notes_log
from app.rawlog.notes import NotesLog

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}/notes", tags=["notes"])


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000,
                      description="The note's text.")
    tags: list[str] = Field(default_factory=list,
                            description="Labels, e.g. ['hiring','follow-up']. "
                                        "Query notes by tag.")
    expires_at: str | None = Field(
        None, description="Optional ISO-8601 expiry. Once due, the note shows "
                          "up in /expired so it is not forgotten.")
    source: str | None = Field(None, description="Evidence, e.g. 'session:2026-08-26:abc'.")


@router.post("", status_code=201)
def create_note(user_id: str, body: NoteIn, notes: NotesLog = Depends(get_notes_log)):
    """File a medium-memory note. No LLM, no routing — the whole point is a
    cheap, immediate write for the short->medium hand-off."""
    return notes.put(user_id, body.model_dump())


@router.get("")
def list_notes(user_id: str, notes: NotesLog = Depends(get_notes_log),
               tag: str | None = Query(None, description="Only notes with this tag."),
               active: bool = Query(False, description="Only non-expired notes."),
               include_expired: bool = Query(True, description="Drop expired "
                                              "unless you want them.")):
    return notes.list(user_id, tag=tag, active=active,
                      include_expired=include_expired)


@router.get("/expired")
def expire_status(user_id: str, notes: NotesLog = Depends(get_notes_log)):
    """Medium-memory dashboard: active/expired counts + the expired notes,
    which is what a sweep would re-surface."""
    return notes.expire_status(user_id)


@router.get("/{note_id}")
def get_note(user_id: str, note_id: str,
             notes: NotesLog = Depends(get_notes_log)):
    note = notes.get(user_id, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail=f"No note {note_id!r}.")
    return note


@router.delete("/{note_id}")
def delete_note(user_id: str, note_id: str,
                notes: NotesLog = Depends(get_notes_log)):
    if not notes.delete(user_id, note_id):
        raise HTTPException(status_code=404, detail=f"No note {note_id!r}.")
    return {"deleted": note_id}
