"""Append-only, recoverable per-session original-event journals."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, Field

from short_term_memory.models import MemoryContentType, MemoryEvent
from short_term_memory.storage.vfs_adapter import VFSAdapter, safe_component


JournalRole = Literal["user", "assistant", "system", "tool", "unknown"]


class JournalEvent(BaseModel):
    type: Literal["message", "file"]
    timestamp: str


class JournalMessageEvent(JournalEvent):
    type: Literal["message"] = "message"
    role: JournalRole
    content: str
    event_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    content_type: MemoryContentType = MemoryContentType.CONVERSATION
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str | None = None


class JournalFileEvent(JournalEvent):
    type: Literal["file"] = "file"
    original_url: str
    local_path: str


JournalRecord = JournalMessageEvent | JournalFileEvent


class JournalConflictError(ValueError):
    """An event ID already exists with a different original-content digest."""


@dataclass(frozen=True)
class JournalAppendResult:
    appended: bool
    path: Path


def _utc_timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must be an ISO-8601 datetime") from error
    return _utc_timestamp(timestamp)


def _text_content(value: str | list[dict[str, object]]) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(
        str(part["text"])
        for part in value
        if part.get("type") == "input_text" and isinstance(part.get("text"), str)
    )


class JournalStore:
    def __init__(self, vfs: VFSAdapter) -> None:
        self.vfs = vfs
        self._locks_guard = RLock()
        self._session_locks: dict[tuple[str, str], RLock] = {}

    def append_message(
        self,
        user_id: str,
        session_id: str,
        *,
        role: JournalRole,
        content: str | list[dict[str, object]],
        timestamp: datetime | None = None,
    ) -> Path:
        at = _utc_timestamp(timestamp)
        event = JournalMessageEvent(
            role=role,
            content=_text_content(content),
            timestamp=at.isoformat(),
        )
        return self._append(user_id, session_id, at, event)

    def append_file(
        self,
        user_id: str,
        session_id: str,
        *,
        original_url: str,
        local_path: str | None,
        timestamp: datetime | None = None,
    ) -> Path:
        at = _utc_timestamp(timestamp)
        event = JournalFileEvent(
            original_url=original_url,
            local_path=local_path or original_url,
            timestamp=at.isoformat(),
        )
        return self._append(user_id, session_id, at, event)

    def append_event(
        self, user_id: str, session_id: str, event: MemoryEvent
    ) -> JournalAppendResult:
        at = _parse_timestamp(event.created_at)
        record = JournalMessageEvent(
            role=event.role,
            content=event.content,
            timestamp=at.isoformat(),
            event_id=event.event_id,
            sequence=event.sequence,
            content_type=event.content_type,
            metadata=dict(event.metadata),
            sha256=event.sha256,
        )
        with self._session_lock(user_id, session_id):
            existing = self._find_event_entry_unlocked(user_id, session_id, event.event_id)
            if existing is not None:
                existing_event, existing_path = existing
                if existing_event.sha256 != event.sha256:
                    raise JournalConflictError(
                        f"event_id {event.event_id!r} already has a different digest"
                    )
                return JournalAppendResult(appended=False, path=existing_path)
            path = self._append_unlocked(user_id, session_id, at, record)
            return JournalAppendResult(appended=True, path=path)

    def find_event(
        self, user_id: str, session_id: str, event_id: str
    ) -> MemoryEvent | None:
        with self._session_lock(user_id, session_id):
            entry = self._find_event_entry_unlocked(user_id, session_id, event_id)
            return entry[0] if entry is not None else None

    def read_original_range(
        self,
        user_id: str,
        session_id: str,
        from_sequence: int,
        through_sequence: int,
    ) -> tuple[MemoryEvent, ...]:
        with self._session_lock(user_id, session_id):
            events = (
                self._memory_event(record)
                for record, _ in self._read_session_entries_unlocked(user_id, session_id)
                if isinstance(record, JournalMessageEvent) and record.sequence is not None
            )
            return tuple(
                event
                for event in events
                if from_sequence <= event.sequence <= through_sequence
            )

    def read_session(self, user_id: str, session_id: str) -> tuple[JournalRecord, ...]:
        with self._session_lock(user_id, session_id):
            return tuple(
                record
                for record, _ in self._read_session_entries_unlocked(user_id, session_id)
            )

    def list_for_day(self, user_id: str, day: str) -> tuple[Path, ...]:
        safe_component(day, "journal day")
        return tuple(sorted(self.vfs.paths(user_id).journals.glob(f"{day}-*.jsonl")))

    def _append(
        self,
        user_id: str,
        session_id: str,
        timestamp: datetime,
        event: JournalRecord,
    ) -> Path:
        with self._session_lock(user_id, session_id):
            return self._append_unlocked(user_id, session_id, timestamp, event)

    def _append_unlocked(
        self,
        user_id: str,
        session_id: str,
        timestamp: datetime,
        event: JournalRecord,
    ) -> Path:
        session = safe_component(session_id, "session_id")
        path = (
            self.vfs.paths(user_id).journals
            / f"{timestamp.date().isoformat()}-{session}.jsonl"
        )
        encoded = json.dumps(
            self._encoded_record(event),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _find_event_entry_unlocked(
        self, user_id: str, session_id: str, event_id: str
    ) -> tuple[MemoryEvent, Path] | None:
        for record, path in self._read_session_entries_unlocked(user_id, session_id):
            if (
                isinstance(record, JournalMessageEvent)
                and record.event_id == event_id
            ):
                return self._memory_event(record), path
        return None

    @staticmethod
    def _encoded_record(event: JournalRecord) -> dict[str, object]:
        encoded = event.model_dump(mode="json", exclude_none=True)
        if isinstance(event, JournalMessageEvent) and event.event_id is None:
            for field in ("content_type", "metadata"):
                encoded.pop(field, None)
        return encoded

    def _read_session_entries_unlocked(
        self, user_id: str, session_id: str
    ) -> tuple[tuple[JournalRecord, Path], ...]:
        session = safe_component(session_id, "session_id")
        directory = self.vfs.paths(user_id).journals
        records: list[tuple[JournalRecord, Path]] = []
        for path in sorted(directory.glob(f"*-{session}.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    if index == len(lines) - 1 and not line.endswith("\n"):
                        continue
                    raise
                if raw.get("type") == "message":
                    records.append((JournalMessageEvent.model_validate(raw), path))
                elif raw.get("type") == "file":
                    records.append((JournalFileEvent.model_validate(raw), path))
                else:
                    raise ValueError(f"unknown journal event type in {path.name}")
        return tuple(records)

    def _session_lock(self, user_id: str, session_id: str) -> RLock:
        key = (
            safe_component(user_id, "user_id"),
            safe_component(session_id, "session_id"),
        )
        with self._locks_guard:
            return self._session_locks.setdefault(key, RLock())

    @staticmethod
    def _memory_event(record: JournalMessageEvent) -> MemoryEvent:
        if (
            record.event_id is None
            or record.sequence is None
            or record.sha256 is None
        ):
            raise ValueError("journal message is not a sequence-bearing original event")
        return MemoryEvent(
            sequence=record.sequence,
            event_id=record.event_id,
            role=record.role,
            content_type=record.content_type,
            content=record.content,
            metadata=record.metadata,
            sha256=record.sha256,
            created_at=record.timestamp,
        )
