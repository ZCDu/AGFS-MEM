import uuid
from pydantic import BaseModel, Field


class ContentItem(BaseModel):
    type: str  # "input_text" | "input_image" | "file"
    text: str | None = None
    image_url: str | None = None
    file_url: str | None = None


class Message(BaseModel):
    role: str
    content: str | list[ContentItem]


class MemorySettings(BaseModel):
    """Per-request memory behavior overrides."""
    recent_rounds_full: int | None = None


class MemoryRequest(BaseModel):
    model: str = "memory-v1"
    userId: str
    sessionId: str
    reasoning: dict | None = None
    memory_settings: MemorySettings | None = None
    input: list[Message]


class RetrievedMemory(BaseModel):
    id: str
    memory: str
    score: float
    created_at: str
    importance: float


class MemoryStoreResponse(BaseModel):
    """Response for /v1/memory/store — lightweight confirmation."""
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: str = "memory.store"
    model: str = "memory-v1"
    status: str = "stored"


class MemoryRecallResponse(BaseModel):
    """Response for /v1/memory/recall — full history + retrieved memories."""
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: str = "memory.recall"
    model: str = "memory-v1"
    history: list[dict] = []
    retrieved_memories: list[RetrievedMemory] = []
    usage: dict = Field(default_factory=lambda: {"total_tokens": 0})
