from fastapi import APIRouter
from memory_system.api.models import MemoryRequest, MemoryResponse

router = APIRouter()

_memory_service = None


def set_memory_service(svc):
    global _memory_service
    _memory_service = svc


def get_memory_service():
    return _memory_service


@router.post("/v1/memory", response_model=MemoryResponse)
async def process_memory(request: MemoryRequest):
    """Process a memory request: store session + retrieve relevant memories."""
    svc = get_memory_service()
    return await svc.process(request)


@router.get("/health")
async def health():
    return {"status": "ok"}
