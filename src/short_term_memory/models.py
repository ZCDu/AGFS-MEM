"""Shared short-term session and compression data models."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

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
