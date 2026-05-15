import uuid
from typing import Literal
from pydantic import BaseModel, Field


# ── Request models ──────────────────────────────────────────────────────────

ContentType = Literal["input_text", "input_image", "file"]
MessageRole = Literal["user", "assistant", "system", "tool"]


class ContentItem(BaseModel):
    type: ContentType
    text: str | None = None
    image_url: str | None = None
    file_url: str | None = None


class Message(BaseModel):
    role: MessageRole
    content: str | list[ContentItem]


class MemorySettings(BaseModel):
    """Per-request memory behavior overrides."""
    recent_rounds_full: int | None = Field(default=None, gt=0)


class MemoryRequest(BaseModel):
    model: str = "memory-v1"
    userId: str = Field(min_length=1)
    sessionId: str = Field(min_length=1)
    reasoning: dict | None = None
    memory_settings: MemorySettings | None = None
    input: list[Message] = Field(min_length=1)


# ── Response models ─────────────────────────────────────────────────────────

class RetrievedMemory(BaseModel):
    id: str
    memory: str
    score: float = Field(ge=0, le=2)  # ES cosine similarity: 1+cosine, range [0,2]
    created_at: str
    importance: float = Field(ge=0, le=2)


class HistoryMessage(BaseModel):
    role: MessageRole
    content: str


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def __iadd__(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        return self


class MemoryStoreResponse(BaseModel):
    """Response for /v1/memory/store — lightweight confirmation."""
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: Literal["memory.store"] = "memory.store"
    model: str = "memory-v1"
    status: Literal["stored"] = "stored"
    usage: Usage = Field(default_factory=Usage)


class MemoryRecallResponse(BaseModel):
    """Response for /v1/memory/recall — full history + retrieved memories."""
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    object: Literal["memory.recall"] = "memory.recall"
    model: str = "memory-v1"
    history: list[HistoryMessage] = []
    retrieved_memories: list[RetrievedMemory] = []
    usage: Usage = Field(default_factory=Usage)
