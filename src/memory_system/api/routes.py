from fastapi import APIRouter, HTTPException
from memory_system.api.models import (
    MemoryRequest,
    MemoryStoreResponse,
    MemoryRecallResponse,
    ContextRetrieveResponse,
    HistoryMessage,
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


@router.get("/v1/memory/context/{context_hash}", response_model=ContextRetrieveResponse)
async def retrieve_context(context_hash: str):
    """Retrieve original session messages behind a compressed history marker."""
    svc = get_memory_service()
    context = await svc.retrieve_context(context_hash)
    if not context:
        raise HTTPException(status_code=404, detail="context not found")
    return ContextRetrieveResponse(
        hash=context["hash"],
        user_id=context["user_id"],
        session_id=context["session_id"],
        round_id=context["round_id"],
        messages=[
            HistoryMessage(role=message["role"], content=str(message.get("content", "")))
            for message in context["messages"]
        ],
        query_text=context.get("query_text", ""),
        created_at=context.get("created_at", ""),
    )


@router.get("/health")
async def health():
    return {"status": "ok"}
