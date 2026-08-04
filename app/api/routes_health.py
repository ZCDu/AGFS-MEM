from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(settings: Settings = Depends(get_settings)):
    return {"status": "ok", "storage_backend": settings.storage_backend}
