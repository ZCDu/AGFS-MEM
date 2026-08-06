"""Redis Session Context with journals-based expiry recovery."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable, Protocol

from short_term_memory.models import SessionSummaryDocument
from short_term_memory.ports import SessionCompressionQueue, SummarySnapshotReader
from short_term_memory.storage.journal_store import (
    JournalFileEvent,
    JournalMessageEvent,
    JournalRecord,
    JournalStore,
)
from short_term_memory.storage.vfs_adapter import safe_component


class RedisClient(Protocol):
    def pipeline(self, *, transaction: bool = True) -> object: ...

    def rpush(self, key: str, value: str) -> object: ...

    def lrange(self, key: str, start: int, end: int) -> list[object]: ...

    def ltrim(self, key: str, start: int, end: int) -> object: ...

    def set(self, key: str, value: str, *, ex: int | None = None) -> object: ...

    def get(self, key: str) -> object | None: ...

    def expire(self, key: str, seconds: int) -> object: ...

    def delete(self, *keys: str) -> object: ...

    def exists(self, *keys: str) -> object: ...

    def llen(self, key: str) -> object: ...


class ContextAttachmentTelemetry(Protocol):
    def record_context_attached(self) -> None: ...


@dataclass(frozen=True)
class CompressionSnapshot:
    messages: tuple[dict[str, Any], ...]
    processed_message_count: int


class RedisSessionContext:
    def __init__(
        self,
        client: RedisClient,
        journal_store: JournalStore,
        *,
        ttl_seconds: int = 43_200,
        snapshot_reader: SummarySnapshotReader | None = None,
        recovery_queue: SessionCompressionQueue | None = None,
        telemetry: ContextAttachmentTelemetry | None = None,
        ccr_ttl_seconds: int = 43_200,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if ccr_ttl_seconds < 1:
            raise ValueError("ccr_ttl_seconds must be positive")
        self.client = client
        self.journal_store = journal_store
        self.ttl_seconds = ttl_seconds
        self.snapshot_reader = snapshot_reader
        self.recovery_queue = recovery_queue
        self.telemetry = telemetry
        self.ccr_ttl_seconds = ccr_ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def append_message(
        self,
        user_id: str,
        session_id: str,
        message: dict[str, str],
    ) -> None:
        key = self._message_key(user_id, session_id)
        summary_key = self._summary_key(user_id, session_id)
        pipe = self.client.pipeline(transaction=True)
        pipe.rpush(
            key,
            json.dumps(message, ensure_ascii=False, separators=(",", ":")),
        )
        pipe.expire(key, self.ttl_seconds)
        pipe.expire(summary_key, self.ttl_seconds)
        pipe.execute()

    def has_session(self, user_id: str, session_id: str) -> bool:
        return bool(
            self.client.exists(
                self._message_key(user_id, session_id),
                self._summary_key(user_id, session_id),
            )
        )

    def recent_messages(
        self, user_id: str, session_id: str, limit: int
    ) -> tuple[dict[str, str], ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        values = self.client.lrange(
            self._message_key(user_id, session_id), -limit, -1
        )
        return tuple(self._decode_message(value) for value in values)

    def recent_turns(
        self, user_id: str, session_id: str, turns: int
    ) -> tuple[dict[str, str], ...]:
        messages = self.recent_messages(user_id, session_id, turns * 2)
        if messages and messages[0]["role"] == "assistant":
            return messages[1:]
        return messages

    def compression_history(
        self, user_id: str, session_id: str
    ) -> tuple[dict[str, Any], ...]:
        values = self.client.lrange(
            self._message_key(user_id, session_id), 0, -1
        )
        messages = tuple(self._decode_message(value) for value in values)
        summary = self.get_summary(user_id, session_id)
        if summary is None:
            return messages
        return (*self._summary_messages(summary), *messages)

    def compression_snapshot(
        self, user_id: str, session_id: str
    ) -> CompressionSnapshot:
        return CompressionSnapshot(
            messages=tuple(
                self._decode_message(value)
                for value in self.client.lrange(
                    self._message_key(user_id, session_id), 0, -1
                )
            ),
            processed_message_count=int(
                self.client.llen(self._message_key(user_id, session_id))
            ),
        )

    def set_summary(self, user_id: str, session_id: str, summary: str) -> None:
        self.client.set(
            self._summary_key(user_id, session_id),
            summary,
            ex=self.ttl_seconds,
        )

    def trim_messages(self, user_id: str, session_id: str, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        key = self._message_key(user_id, session_id)
        self.client.ltrim(key, -limit, -1)
        self.client.expire(key, self.ttl_seconds)

    def store_compression_result(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None:
        if processed_message_count < 0:
            raise ValueError("processed_message_count must not be negative")
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be positive")
        message_key = self._message_key(user_id, session_id)
        summary_key = self._summary_key(user_id, session_id)
        trim_start = max(0, processed_message_count - 2 * keep_recent_turns)
        pipe = self.client.pipeline(transaction=True)
        pipe.set(summary_key, summary, ex=self.ttl_seconds)
        pipe.ltrim(message_key, trim_start, -1)
        pipe.expire(message_key, self.ttl_seconds)
        pipe.execute()

    def get_summary(self, user_id: str, session_id: str) -> str | None:
        value = self.client.get(self._summary_key(user_id, session_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def build_history(
        self, user_id: str, session_id: str, turns: int
    ) -> tuple[dict[str, Any], ...]:
        messages = self.recent_turns(user_id, session_id, turns)
        summary = self.get_summary(user_id, session_id)
        if summary is None:
            return messages
        return (*self._summary_messages(summary), *messages)

    def ensure_session_loaded(
        self, user_id: str, session_id: str, turns: int
    ) -> None:
        if not self.has_session(user_id, session_id):
            self.restore_from_journals(user_id, session_id, turns)

    def restore_from_journals(
        self, user_id: str, session_id: str, turns: int
    ) -> tuple[dict[str, str], ...]:
        records = self.journal_store.read_session(user_id, session_id)
        summary = (
            self.snapshot_reader.read(user_id, session_id)
            if self.snapshot_reader is not None
            else None
        )
        recent_count = turns * 2
        message_positions = tuple(
            index
            for index, record in enumerate(records)
            if isinstance(record, JournalMessageEvent)
        )
        recent_start = (
            message_positions[-recent_count]
            if len(message_positions) > recent_count
            else 0
        )
        older_records = records[:recent_start]
        recent_records = records[recent_start:]
        if summary is not None:
            self.set_summary(user_id, session_id, summary)
        messages = tuple(self._journal_message(record) for record in recent_records)
        for message in messages:
            self.append_message(user_id, session_id, message)
        if (
            older_records
            and self.recovery_queue is not None
            and self._snapshot_needs_rebuild(summary)
        ):
            self.recovery_queue.enqueue(
                user_id,
                session_id,
                tuple(self._journal_message(record) for record in older_records),
                0,
                turns,
            )
        return messages

    def delete(self, user_id: str, session_id: str) -> None:
        self.client.delete(
            self._message_key(user_id, session_id),
            self._summary_key(user_id, session_id),
        )

    @staticmethod
    def _decode_message(value: object) -> dict[str, str]:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        decoded = json.loads(str(value))
        return {"role": str(decoded["role"]), "content": str(decoded["content"])}

    @staticmethod
    def _journal_message(record: JournalRecord) -> dict[str, str]:
        if isinstance(record, JournalFileEvent):
            return {
                "role": "system",
                "content": f"[attachment: {record.local_path}]",
            }
        return {"role": record.role, "content": record.content}

    def _summary_messages(self, value: str) -> tuple[dict[str, Any], ...]:
        document = self._structured_summary(value)
        if document is None:
            return ({"role": "system", "content": value},)
        semantic = document.model_dump(
            include={
                "current_goal",
                "preferences",
                "confirmed_facts",
                "pending_items",
                "attachment_references",
            }
        )
        result: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": json.dumps(
                    semantic,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ]
        context_is_fresh = self._compression_context_is_fresh(document)
        if context_is_fresh:
            result.extend(
                message.model_dump(exclude_none=True)
                for message in document.compression_context.messages
            )
        if (
            context_is_fresh
            and document.compression_context.messages
            and self.telemetry is not None
        ):
            self.telemetry.record_context_attached()
        return tuple(result)

    @staticmethod
    def _structured_summary(value: str) -> SessionSummaryDocument | None:
        try:
            return SessionSummaryDocument.model_validate_json(value)
        except (TypeError, ValueError):
            return None

    def _compression_context_is_fresh(
        self, document: SessionSummaryDocument
    ) -> bool:
        if not document.compression_context.messages:
            return True
        updated_at = datetime.fromisoformat(
            document.updated_at.replace("Z", "+00:00")
        )
        now = self.clock()
        if (
            updated_at.tzinfo is None
            or updated_at.utcoffset() is None
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return False
        return now - updated_at <= timedelta(seconds=self.ccr_ttl_seconds)

    def _snapshot_needs_rebuild(self, value: str | None) -> bool:
        if value is None:
            return True
        document = self._structured_summary(value)
        return bool(
            document is not None
            and document.compression_context.messages
            and not self._compression_context_is_fresh(document)
        )

    @staticmethod
    def _message_key(user_id: str, session_id: str) -> str:
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        return f"dream:session:{user}:{session}:messages"

    @staticmethod
    def _summary_key(user_id: str, session_id: str) -> str:
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        return f"dream:session:{user}:{session}:summary"
