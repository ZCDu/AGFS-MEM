"""
Raw fact/message log, stored as JSONL — but sharded by user+day rather than one
ever-growing file, because S3 objects aren't appendable (writing means
re-uploading the whole object). One file per user per day keeps each
read-modify-write cheap and gives you natural retention/compaction boundaries
later (e.g. "facts older than 90 days get compacted or summarized").

Key layout: {user_id}/raw-facts/{YYYY-MM-DD}.jsonl

Note: this is a distinct concept from PLAN.md's <user_id>/raw/ directory
(that's for original uploaded file attachments; this is an append-only log
of extracted fact records) — named raw-facts/ specifically to avoid
colliding with or being confused for that.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from app.storage.backend import ConflictError, StorageBackend

MAX_WRITE_RETRIES = 5


class RawFactLog:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    def _key(self, user_id: str, day: date) -> str:
        return f"{user_id}/raw-facts/{day.isoformat()}.jsonl"

    def append(self, user_id: str, record: dict, when: datetime | None = None) -> None:
        """Append a single record. Prefer append_batch() when writing several at once —
        this does one read-modify-write per call, so N sequential calls means N round trips."""
        self.append_batch(user_id, [record], when=when)

    def append_batch(self, user_id: str, records: list[dict], when: datetime | None = None) -> None:
        if not records:
            return
        when = when or datetime.now(timezone.utc)
        day = when.date()
        key = self._key(user_id, day)

        for record in records:
            record.setdefault("ts", when.isoformat())

        last_error = None
        for _ in range(MAX_WRITE_RETRIES):
            current = self.backend.get_bytes(key)
            existing_lines = current.data.decode("utf-8").splitlines() if current else []
            new_lines = [json.dumps(r, ensure_ascii=False) for r in records]
            payload = "\n".join(existing_lines + new_lines) + "\n"
            etag = current.etag if current else ""
            try:
                self.backend.put_bytes(key, payload.encode("utf-8"), if_match=etag)
                return
            except ConflictError as e:
                last_error = e
                continue
        raise last_error or RuntimeError("Failed to append to raw log after retries")

    def read_day(self, user_id: str, day: date) -> list[dict]:
        raw = self.backend.get_bytes(self._key(user_id, day))
        if raw is None:
            return []
        return [json.loads(line) for line in raw.data.decode("utf-8").splitlines() if line.strip()]

    def iter_range(self, user_id: str, start: date, end: date):
        """Yields records across [start, end] inclusive, one day-shard at a time."""
        from datetime import timedelta
        d = start
        while d <= end:
            yield from self.read_day(user_id, d)
            d += timedelta(days=1)
