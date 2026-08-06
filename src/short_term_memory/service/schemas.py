"""Pydantic contracts for the two HTTP memory-service APIs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from short_term_memory.models import (
    JournalRole,
    MemoryContentType,
    SessionCompressionMessage,
)


class MemoryEventInput(BaseModel):
    """Client fields for an original event; server fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=200)
    role: JournalRole
    content_type: MemoryContentType
    content: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)


class MemoryWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    events: list[MemoryEventInput] = Field(min_length=1)
    session_seconds: int = Field(default=0, ge=0)


class WriteTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: float = Field(ge=0)
    redis: float = Field(ge=0)
    journal: float = Field(ge=0)
    queue: float = Field(ge=0)


class MemoryWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    accepted: bool
    sequence_from: int | None = Field(default=None, ge=1)
    sequence_through: int | None = Field(default=None, ge=1)
    duplicate_event_ids: list[str] = Field(default_factory=list)
    compression_queued: bool
    policy_version: str = Field(min_length=1)
    timing_ms: WriteTiming


class MemoryReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    history_turns: int | None = Field(default=None, ge=1)
    include_effective_config: bool = False


class MemoryReadState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    compressed_through_sequence: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    source: Literal["redis", "journal_rebuild"]
    compression_segments: int = Field(ge=0)


class HeadroomProxyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proxy_url: str = Field(min_length=1)
    scope_headers: dict[str, str]


class EffectiveMemoryConfig(BaseModel):
    """Only non-secret configuration that callers may receive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    history_turns: int = Field(ge=1)
    redis_ttl_seconds: int = Field(ge=1)
    ccr_ttl_seconds: int = Field(ge=1)
    journal_retention_days: int = Field(ge=1)
    trigger_ratio: float = Field(ge=0, le=1)
    policy_version: str = Field(min_length=1)


class ReadTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: float = Field(ge=0)
    redis: float = Field(ge=0)
    recovery: float = Field(ge=0)
    assembly: float = Field(ge=0)


class MemoryReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    messages: list[SessionCompressionMessage]
    memory: MemoryReadState
    headroom: HeadroomProxyContext
    effective_config: EffectiveMemoryConfig | None
    timing_ms: ReadTiming
