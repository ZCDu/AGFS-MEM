"""Append-only per-session journals in the PLAN.md JSONL format."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel

from short_term_memory.storage.vfs_adapter import VFSAdapter, safe_component


JournalRole = Literal["user", "assistant", "system", "tool", "unknown"]


class JournalEvent(BaseModel):
    type: Literal["message", "file"]
    timestamp: str


class JournalMessageEvent(JournalEvent):
    type: Literal["message"] = "message"
    role: JournalRole
    content: str


class JournalFileEvent(JournalEvent):
    type: Literal["file"] = "file"
    original_url: str
    local_path: str


JournalRecord = JournalMessageEvent | JournalFileEvent


def _utc_timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


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
        self._lock = RLock()

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

    def read_session(self, user_id: str, session_id: str) -> tuple[JournalRecord, ...]:
        session = safe_component(session_id, "session_id")
        directory = self.vfs.paths(user_id).journals
        records: list[JournalRecord] = []
        for path in sorted(directory.glob(f"*-{session}.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if raw.get("type") == "message":
                        records.append(JournalMessageEvent.model_validate(raw))
                    elif raw.get("type") == "file":
                        records.append(JournalFileEvent.model_validate(raw))
                    else:
                        raise ValueError(f"unknown journal event type in {path.name}")
        return tuple(records)

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
        session = safe_component(session_id, "session_id")
        path = (
            self.vfs.paths(user_id).journals
            / f"{timestamp.date().isoformat()}-{session}.jsonl"
        )
        encoded = json.dumps(
            event.model_dump(), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return path
