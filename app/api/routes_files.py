"""
Raw file upload.

Files are stored byte-for-byte and text is extracted separately, on demand.
The archival copy is what makes later improvements to extraction — or a PDF
reader, which does not exist yet — applicable to files already uploaded.

Files are organized by session: {user_id}/raw/{session_id}/{file_id}/
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.auth import require_user
from app.deps import get_storage_backend
from app.rawlog.files import FileError, FileStore

logger = logging.getLogger("memory_backend.files")

router = APIRouter(dependencies=[Depends(require_user)],
                   prefix="/v1/users/{user_id}/files", tags=["files"])

# Guards the request, not the storage: an unbounded upload is an unbounded
# read into memory before anything else gets a chance to reject it.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _store() -> FileStore:
    return FileStore(get_storage_backend())


@router.post("")
async def upload(
    user_id: str,
    file: UploadFile = File(..., description="The file to store"),
    session_id: str = Form("", description="Conversation session it belongs to"),
):
    """Store a file and report whether its text can be given to the model."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data)} bytes; the limit is {MAX_UPLOAD_BYTES}.")

    meta = _store().save(user_id, file.filename or "unnamed", data,
                         content_type=file.content_type or "",
                         session_id=session_id)
    return meta.to_dict()


@router.get("")
def list_files(user_id: str, session_id: str = Query("", description="Session to list files for")):
    """List files in a session, or list all sessions if no session_id given."""
    store = _store()
    if session_id:
        return [m.to_dict() for m in store.list_session(user_id, session_id)]
    return store.list_sessions(user_id)


@router.get("/{file_id}")
def get_file_meta(user_id: str, file_id: str,
                  session_id: str = Query("", description="Session the file belongs to")):
    store = _store()
    try:
        sid = session_id or store.find(user_id, file_id)
        if sid is None:
            raise HTTPException(status_code=404, detail="No such file.")
        meta = store.get_meta(user_id, file_id, sid)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return meta.to_dict()


@router.get("/{file_id}/text")
def get_file_text(user_id: str, file_id: str,
                  session_id: str = Query("", description="Session the file belongs to")):
    """Text as the model would see it, re-derived from the stored bytes rather
    than from a cached copy — so improvements to extraction apply to files
    uploaded before them."""
    store = _store()
    try:
        sid = session_id or store.find(user_id, file_id)
        if sid is None:
            raise HTTPException(status_code=404, detail="No such file.")
        text, meta = store.get_text(user_id, file_id, sid)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return {"file_id": file_id, "name": meta.name, "session_id": meta.session_id,
            "text_extractable": meta.text_extractable,
            "note": meta.note, "text": text}


@router.get("/{file_id}/content")
def download(user_id: str, file_id: str,
             session_id: str = Query("", description="Session the file belongs to")):
    """The original bytes, unmodified."""
    store = _store()
    try:
        sid = session_id or store.find(user_id, file_id)
        if sid is None:
            raise HTTPException(status_code=404, detail="No such file.")
        meta = store.get_meta(user_id, file_id, sid)
        data = store.get_bytes(user_id, file_id, sid)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if data is None or meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return Response(
        content=data,
        media_type=meta.content_type or "application/octet-stream",
        headers={"content-disposition": f'attachment; filename="{meta.name}"'})
