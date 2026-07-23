"""Provider-independent models for runtime memory retrieval."""

from dataclasses import dataclass, field
from enum import StrEnum


class MemoryKind(StrEnum):
    USER_PERSONA = "user_persona"
    DECISION_CARD = "decision_card"
    SKILL_CANDIDATE = "skill_candidate"


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    kind: MemoryKind
    content: str
    tenant_id: str
    agent_id: str
    user_id: str | None = None
    confidence: float = 1.0
    source_event_ids: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (
            ("memory_id", self.memory_id),
            ("tenant_id", self.tenant_id),
            ("agent_id", self.agent_id),
            ("content", self.content),
        ):
            if not value.strip():
                raise ValueError(f"{label} must be non-empty")
        if isinstance(self.confidence, bool) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between zero and one")


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    tenant_id: str
    agent_id: str
    user_id: str
    kinds: tuple[MemoryKind, ...] = ()
    limit: int = 8

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        if not self.agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        if not self.user_id.strip():
            raise ValueError("user_id must be non-empty")
        if self.limit < 1:
            raise ValueError("retrieval limit must be positive")


@dataclass(frozen=True)
class RankedMemory:
    record: MemoryRecord
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    query: RetrievalQuery
    matches: tuple[RankedMemory, ...]


@dataclass(frozen=True)
class RetrievedContext:
    markdown: str
    included_memory_ids: tuple[str, ...]
    estimated_tokens: int
