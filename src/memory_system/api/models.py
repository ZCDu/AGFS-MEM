import uuid
from pydantic import BaseModel, Field


class ContentItem(BaseModel):
    type: str  # "input_text" | "input_image"
    text: str | None = None
    image_url: str | None = None


class Message(BaseModel):
    role: str
    content: str | list[ContentItem]


class MemoryRequest(BaseModel):
    model: str = "memory-v1"
    userId: str
    sessionId: str
    reasoning: dict | None = None
    input: list[Message]


class RetrievedMemory(BaseModel):
    id: str
    memory: str
    score: float
    created_at: str
    importance: float


class MemoryResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: str = "memory.response"
    model: str = "memory-v1"
    output_text: str = ""
    history: list[dict] = []
    retrieved_memories: list[RetrievedMemory] = []
    usage: dict = Field(default_factory=lambda: {"total_tokens": 0})
