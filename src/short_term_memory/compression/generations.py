"""Original-only compression planning and read-time generation assembly."""

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Protocol

from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.storage.journal_store import JournalStore


class OriginalMemoryStore(Protocol):
    async def read_originals_after(
        self, user_id: str, session_id: str, sequence: int
    ) -> tuple[MemoryEvent, ...]: ...

    async def read_envelope(
        self, user_id: str, session_id: str
    ) -> MemorySummaryEnvelope | None: ...


@dataclass(frozen=True)
class CompressionCandidate:
    """A CAS-bound range of journal-original events for one compression call."""

    user_id: str
    session_id: str
    expected_version: int
    from_sequence: int
    through_sequence: int
    originals: tuple[MemoryEvent, ...]
    rebuild: bool


class GenerationPlanner:
    """Choose source ranges without ever consulting prior Headroom output."""

    def __init__(
        self, store: OriginalMemoryStore, journals: JournalStore, *, max_segments: int
    ) -> None:
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        self.store = store
        self.journals = journals
        self.max_segments = max_segments

    async def plan_incremental(
        self, user_id: str, session_id: str
    ) -> CompressionCandidate | None:
        envelope = await self.store.read_envelope(user_id, session_id)
        through = envelope.compressed_through_sequence if envelope else 0
        originals = self._deduplicate_by_sequence(
            await self.store.read_originals_after(user_id, session_id, through)
        )
        if not originals:
            return None
        return CompressionCandidate(
            user_id=user_id,
            session_id=session_id,
            expected_version=envelope.version if envelope else 0,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=False,
        )

    async def plan_rebuild(
        self, user_id: str, session_id: str, through_sequence: int
    ) -> CompressionCandidate | None:
        if through_sequence < 1:
            raise ValueError("through_sequence must be positive")
        envelope = await self.store.read_envelope(user_id, session_id)
        originals = self._deduplicate_by_sequence(
            self.journals.read_original_range(user_id, session_id, 1, through_sequence)
        )
        if not originals:
            return None
        return CompressionCandidate(
            user_id=user_id,
            session_id=session_id,
            expected_version=envelope.version if envelope else 0,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=True,
        )

    @staticmethod
    def _deduplicate_by_sequence(
        originals: tuple[MemoryEvent, ...]
    ) -> tuple[MemoryEvent, ...]:
        unique: dict[int, MemoryEvent] = {}
        for event in originals:
            unique.setdefault(event.sequence, event)
        return tuple(unique[sequence] for sequence in sorted(unique))


class GenerationAssembler:
    """Assemble a read context while preserving opaque generation messages."""

    def __init__(self, *, max_segments: int) -> None:
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        self.max_segments = max_segments

    def build_read_messages(
        self,
        envelope: MemorySummaryEnvelope | None,
        recent_originals: tuple[MemoryEvent, ...],
        now: datetime,
    ) -> tuple[dict[str, Any], ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        result: list[dict[str, Any]] = []
        if envelope is not None:
            result.append(self._semantic_summary(envelope))
            for generation in self._fresh_generations(envelope, now):
                result.extend(
                    message.model_dump(mode="json", exclude_none=True)
                    for message in generation.messages
                )
        # Recent originals deliberately remain even when their sequence overlaps a
        # compressed range: this is the approved read-context overlap policy.
        result.extend(self._event_message(event) for event in recent_originals)
        return tuple(result)

    def _fresh_generations(
        self, envelope: MemorySummaryEnvelope, now: datetime
    ) -> tuple:
        fresh = tuple(
            generation
            for generation in envelope.compression_generations
            if self._expires_at(generation.ccr_expires_at) > now
        )
        return fresh[-self.max_segments :]

    @staticmethod
    def _expires_at(value: str) -> datetime:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("ccr_expires_at must be timezone-aware")
        return expires_at

    @staticmethod
    def _semantic_summary(envelope: MemorySummaryEnvelope) -> dict[str, str]:
        semantic = {
            "current_goal": list(envelope.current_goal),
            "preferences": list(envelope.preferences),
            "confirmed_facts": list(envelope.confirmed_facts),
            "pending_items": list(envelope.pending_items),
            "attachment_references": [
                attachment.model_dump(mode="json")
                for attachment in envelope.attachment_references
            ],
        }
        return {
            "role": "system",
            "content": json.dumps(semantic, ensure_ascii=False, separators=(",", ":")),
        }

    @staticmethod
    def _event_message(event: MemoryEvent) -> dict[str, str]:
        return {"role": event.role.value, "content": event.content}
