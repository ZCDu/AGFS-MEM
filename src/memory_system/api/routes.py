from fastapi import APIRouter
from memory_system.api.models import (
    MemoryRequest,
    MemoryStoreResponse,
    MemoryRecallResponse,
)

router = APIRouter()

_memory_service = None


def set_memory_service(svc):
    global _memory_service
    _memory_service = svc


def get_memory_service():
    return _memory_service


@router.post("/v1/memory/store", response_model=MemoryStoreResponse)
async def store_memory(request: MemoryRequest):
    """Save a conversation round to session memory."""
    svc = get_memory_service()
    return await svc.store(request)


@router.post("/v1/memory/recall", response_model=MemoryRecallResponse)
async def recall_memory(request: MemoryRequest):
    """Retrieve session history + long-term memories for the current query."""
    svc = get_memory_service()
    return await svc.recall(request)


@router.get("/health")
async def health():
    return {"status": "ok"}
