"""
Raw file upload.

Files are stored byte-for-byte and text is extracted separately, on demand.
The archival copy is what makes later improvements to extraction — or a PDF
reader, which does not exist yet — applicable to files already uploaded.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

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
    session_id: str | None = Form(None, description="Conversation it belongs to"),
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
def list_files(user_id: str, on: date | None = Query(None)):
    """Metadata only — listing a day must not mean downloading every file."""
    day = on or datetime.now(timezone.utc).date()
    return [m.to_dict() for m in _store().list_day(user_id, day)]


@router.get("/{file_id}")
def get_file_meta(user_id: str, file_id: str, on: date | None = Query(None)):
    store = _store()
    try:
        day = on or store.find(user_id, file_id)
        if day is None:
            raise HTTPException(status_code=404, detail="No such file.")
        meta = store.get_meta(user_id, file_id, day)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return meta.to_dict()


@router.get("/{file_id}/text")
def get_file_text(user_id: str, file_id: str, on: date | None = Query(None)):
    """Text as the model would see it, re-derived from the stored bytes rather
    than from a cached copy — so improvements to extraction apply to files
    uploaded before them."""
    store = _store()
    try:
        day = on or store.find(user_id, file_id)
        if day is None:
            raise HTTPException(status_code=404, detail="No such file.")
        text, meta = store.get_text(user_id, file_id, day)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return {"file_id": file_id, "name": meta.name,
            "text_extractable": meta.text_extractable,
            "note": meta.note, "text": text}


@router.get("/{file_id}/content")
def download(user_id: str, file_id: str, on: date | None = Query(None)):
    """The original bytes, unmodified."""
    store = _store()
    try:
        day = on or store.find(user_id, file_id)
        if day is None:
            raise HTTPException(status_code=404, detail="No such file.")
        meta = store.get_meta(user_id, file_id, day)
        data = store.get_bytes(user_id, file_id, day)
    except FileError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if data is None or meta is None:
        raise HTTPException(status_code=404, detail="No such file.")
    return Response(
        content=data,
        media_type=meta.content_type or "application/octet-stream",
        headers={"content-disposition": f'attachment; filename="{meta.name}"'})
