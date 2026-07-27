"""Redis Session Context and Headroom state from PLAN.md."""

from dataclasses import dataclass
import json
from typing import Protocol

from dream.storage.journal_store import JournalMessageEvent, JournalStore
from dream.storage.vfs_adapter import safe_component


class RedisClient(Protocol):
    def rpush(self, key: str, value: str) -> object: ...

    def lrange(self, key: str, start: int, end: int) -> list[object]: ...

    def set(self, key: str, value: str) -> object: ...

    def get(self, key: str) -> object | None: ...

    def expire(self, key: str, seconds: int) -> object: ...

    def delete(self, *keys: str) -> object: ...


@dataclass(frozen=True)
class HeadroomPolicy:
    context_window_tokens: int
    trigger_ratio: float
    max_messages: int
    max_session_seconds: int

    def __post_init__(self) -> None:
        if not 0.60 <= self.trigger_ratio <= 0.70:
            raise ValueError("trigger_ratio must be between 0.60 and 0.70")
        if self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if self.max_messages < 1:
            raise ValueError("max_messages must be positive")
        if self.max_session_seconds < 1:
            raise ValueError("max_session_seconds must be positive")

    def should_compress(
        self,
        *,
        estimated_tokens: int,
        message_count: int,
        session_seconds: int,
    ) -> bool:
        return (
            estimated_tokens
            >= self.context_window_tokens * self.trigger_ratio
            or message_count >= self.max_messages
            or session_seconds >= self.max_session_seconds
        )


class RedisSessionContext:
    def __init__(
        self,
        client: RedisClient,
        journal_store: JournalStore,
        *,
        ttl_seconds: int = 43_200,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.client = client
        self.journal_store = journal_store
        self.ttl_seconds = ttl_seconds

    def append_message(
        self,
        user_id: str,
        session_id: str,
        message: dict[str, str],
    ) -> None:
        key = self._message_key(user_id, session_id)
        self.client.rpush(
            key,
            json.dumps(message, ensure_ascii=False, separators=(",", ":")),
        )
        self.client.expire(key, self.ttl_seconds)

    def recent_messages(
        self, user_id: str, session_id: str, limit: int
    ) -> tuple[dict[str, str], ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        values = self.client.lrange(
            self._message_key(user_id, session_id), -limit, -1
        )
        return tuple(self._decode_message(value) for value in values)

    def set_summary(self, user_id: str, session_id: str, summary: str) -> None:
        key = self._summary_key(user_id, session_id)
        self.client.set(key, summary)
        self.client.expire(key, self.ttl_seconds)

    def get_summary(self, user_id: str, session_id: str) -> str | None:
        value = self.client.get(self._summary_key(user_id, session_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def build_history(
        self, user_id: str, session_id: str, limit: int
    ) -> tuple[dict[str, str], ...]:
        messages = self.recent_messages(user_id, session_id, limit)
        if not messages:
            messages = self.restore_from_journals(user_id, session_id, limit)
        summary = self.get_summary(user_id, session_id)
        if summary is None:
            return messages
        return ({"role": "system", "content": summary}, *messages)

    def restore_from_journals(
        self, user_id: str, session_id: str, limit: int
    ) -> tuple[dict[str, str], ...]:
        records = tuple(
            record
            for record in self.journal_store.read_session(user_id, session_id)
            if isinstance(record, JournalMessageEvent)
        )[-limit:]
        messages = tuple(
            {"role": record.role, "content": record.content}
            for record in records
        )
        for message in messages:
            self.append_message(user_id, session_id, message)
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
    def _message_key(user_id: str, session_id: str) -> str:
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        return f"dream:session:{user}:{session}:messages"

    @staticmethod
    def _summary_key(user_id: str, session_id: str) -> str:
        user = safe_component(user_id, "user_id")
        session = safe_component(session_id, "session_id")
        return f"dream:session:{user}:{session}:summary"
