"""Shared short-term session and compression data models."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HeadroomCompressionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class HeadroomFailureReason(str, Enum):
    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    UNEXPECTED_ERROR = "unexpected_error"


class JournalRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MemoryContentType(str, Enum):
    CONVERSATION = "conversation"
    CODE = "code"
    DOCUMENT = "document"
    SKILL = "skill"


class FrozenMetadata(dict[str, str]):
    """A serializable mapping that rejects post-validation mutation."""

    def __setitem__(self, key: str, value: str) -> None:
        raise TypeError("metadata is immutable")

    def __delitem__(self, key: str) -> None:
        raise TypeError("metadata is immutable")

    def clear(self) -> None:
        raise TypeError("metadata is immutable")

    def pop(self, key: str, default: str | None = None) -> str:
        raise TypeError("metadata is immutable")

    def popitem(self) -> tuple[str, str]:
        raise TypeError("metadata is immutable")

    def setdefault(self, key: str, default: str | None = None) -> str:
        raise TypeError("metadata is immutable")

    def update(self, *args: object, **kwargs: str) -> None:
        raise TypeError("metadata is immutable")

    def __ior__(self, other: object) -> "FrozenMetadata":
        raise TypeError("metadata is immutable")


@dataclass(frozen=True)
class HeadroomCompressionResult:
    status: HeadroomCompressionStatus
    messages: tuple[dict[str, Any], ...]
    fallback_used: bool
    compression_applied: bool = False
    transforms_applied: tuple[str, ...] = ()
    tokens_before: int | None = None
    tokens_after: int | None = None
    tokens_saved: int | None = None
    failure_reason: HeadroomFailureReason | None = None


class SessionAttachmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placeholder: str = Field(min_length=1)
    raw_ref: str = Field(min_length=1)
    source_ref: str | None = None

    @model_validator(mode="after")
    def validate_storage_references(self) -> "SessionAttachmentReference":
        if not self.raw_ref.startswith("raw/"):
            raise ValueError("raw_ref must start with raw/")
        if self.source_ref is not None and not self.source_ref.startswith("source/"):
            raise ValueError("source_ref must start with source/")
        return self


class SessionSummaryPayload(BaseModel):
    """The five semantic categories owned by short-term memory."""

    model_config = ConfigDict(extra="forbid")

    current_goal: list[str]
    preferences: list[str]
    confirmed_facts: list[str]
    pending_items: list[str]
    attachment_references: list[SessionAttachmentReference]

    @field_validator(
        "current_goal",
        "preferences",
        "confirmed_facts",
        "pending_items",
    )
    @classmethod
    def validate_non_blank_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("summary list items must not be blank")
        return [value.strip() for value in values]


class SessionSummaryCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processed_message_count: int = Field(ge=0)


class SessionCompressionMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = Field(min_length=1)
    content: Any = None


class MemoryEvent(BaseModel):
    """An immutable original event persisted by the memory service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=200)
    role: JournalRole
    content_type: MemoryContentType
    content: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=1)

    @field_validator("metadata")
    @classmethod
    def freeze_metadata(cls, value: dict[str, str]) -> FrozenMetadata:
        return FrozenMetadata(value)


class EventReservation(BaseModel):
    """The sequence and state assigned by an atomic idempotency reservation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    state: Literal["reserved", "pending", "committed"]


class CompressionGeneration(BaseModel):
    """An opaque Headroom compression result over one original-event range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=1)
    from_sequence: int = Field(ge=1)
    through_sequence: int = Field(ge=1)
    messages: tuple[SessionCompressionMessage, ...]
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    created_at: str = Field(min_length=1)
    ccr_expires_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "CompressionGeneration":
        if self.through_sequence < self.from_sequence:
            raise ValueError("through_sequence must be >= from_sequence")
        return self


class MemorySummaryEnvelope(SessionSummaryPayload):
    """Semantic summary plus opaque Headroom compression generations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    compressed_through_sequence: int = Field(ge=0)
    compression_generations: tuple[CompressionGeneration, ...] = Field(
        default_factory=tuple
    )
    updated_at: str = Field(min_length=1)


class SessionCompressionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[SessionCompressionMessage] = Field(default_factory=list)
    tokens_before: int | None = Field(default=None, ge=0)
    tokens_after: int | None = Field(default=None, ge=0)


class SessionSummaryDocument(SessionSummaryPayload):
    """Redis summary envelope; it is not a Wiki fact source."""

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    coverage: SessionSummaryCoverage
    compression_context: SessionCompressionContext = Field(
        default_factory=SessionCompressionContext
    )
    updated_at: str = Field(min_length=1)


@dataclass(frozen=True)
class PreparedTurn:
    user_id: str
    session_id: str
    history: tuple[dict[str, Any], ...]
    timestamp: datetime | None
    session_seconds: int
    headroom_headers: dict[str, str]
    headroom_proxy_url: str | None


@dataclass(frozen=True)
class CompletionResult:
    headroom_queued: bool


@dataclass(frozen=True)
class HeadroomJobResult:
    compression_status: HeadroomCompressionStatus
    fallback_used: bool
    summary_written: bool
    compression_applied: bool = False
    failure_reason: HeadroomFailureReason | str | None = None
