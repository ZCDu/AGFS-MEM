"""
Wiki operation log — §8 / ADR-005 of PLAN.md.

One record per wiki write. Deliberately lightweight per ADR-005 — op
type, wiki_id, evidence, reason, timestamp. No before/after diff.

STORAGE LAYOUT — segments, not one appended file
------------------------------------------------
Object stores aren't appendable, so the original design (one file per
day, read-modify-write on every op) rewrote the entire day's log for
every single operation. That is quadratic: the Nth op of the day writes
N lines, so a day with N ops writes O(N^2) bytes to persist O(N) bytes
of log. Measured on this codebase, 200 ops produced 3.5 MB of writes for
a 35 KB result.

Records are now written as immutable segments:

    wikis/{wiki_id}/_ops/{YYYY-MM-DD}/{timestamp}-{suffix}.jsonl

Each flush is one PUT of only the new lines — no read, no rewrite. Total
bytes written is O(N), and the GET per op disappears entirely.

`read_day()` reads every segment for that date, plus the legacy
`{YYYY-MM-DD}_op.jsonl` file if one exists, so logs written by the old
layout are still readable. `compact_day()` folds a day's segments back
into a single object once the day is closed, to keep the read path from
degrading into thousands of small GETs.

WRITE MODES (OPS_LOG_WRITE_MODE)
--------------------------------
`buffered` — (default) accumulate records in memory, flush as one
             segment on a timer / count threshold. Cheapest: many ops
             collapse into a single PUT. A hard crash can lose up to one
             flush window of AUDIT records.
`sync`     — one segment PUT per op, written before the call returns.
             Still avoids the read and the quadratic rewrite, but costs
             a PUT per op. Use this if losing audit lines is
             unacceptable for your compliance posture.
"""

from __future__ import annotations

from app.graph.keys import wiki_key, wiki_prefix

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.storage.backend import StorageBackend
from app.storage.writebehind import FlushBuffer

OpType = str  # "create" | "update_fact" | "update_relation" | "merge" | "delete"

# Compacted single-file form. Built with wiki_key like everything else:
# a constant that hardcodes the prefix is exactly the kind of site that
# gets missed when the layout moves.
_LEGACY_DAY_SUFFIX = "_ops/{day}_op.jsonl"


@dataclass
class WikiOp:
    op_id: str
    op: OpType
    wiki_id: str
    reason: str = ""
    # Who performed the write. In the user-scoped API this is the same as the
    # scope (a token is bound to its path user), but on a shared wiki it is
    # the caller's identity from the token — the field that lets the activity
    # feed show "alice added a fact" rather than "some user did something".
    actor: str = ""
    field_name: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    evidence: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        d = {
            "op_id": self.op_id,
            "op": self.op,
            "wiki_id": self.wiki_id,
            "reason": self.reason,
            "evidence": self.evidence,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
        }
        if self.actor:
            d["actor"] = self.actor
        if self.field_name is not None:
            d["field"] = self.field_name
        if self.before_hash is not None:
            d["before_hash"] = self.before_hash
        if self.after_hash is not None:
            d["after_hash"] = self.after_hash
        return d


class WikiOpsLog(FlushBuffer):
    def __init__(self, backend: StorageBackend, mode: str = "buffered",
                 flush_interval: float = 2.0, max_pending: int = 100):
        super().__init__(flush_interval=flush_interval, max_pending=max_pending)
        self.backend = backend
        self.mode = mode
        # (user_id, day_iso) -> list of serialized lines awaiting flush
        self._buf: dict[tuple[str, str], list[str]] = {}
        # Process-wide monotonic counter. Previously this lived on the
        # instance, but EntityGraphStore is constructed per request (see
        # app/deps.py), so it reset to 0 constantly and contributed no
        # uniqueness at all. Class-level + pid keeps op_ids distinct.
        self._id_lock = threading.Lock()

    _counter = 0
    _pid = os.getpid()

    def _segment_key(self, user_id: str, day: date, suffix: str) -> str:
        return wiki_key(user_id, f"_ops/{day.isoformat()}/{suffix}.jsonl")

    def _legacy_key(self, user_id: str, day: date) -> str:
        return wiki_key(user_id, _LEGACY_DAY_SUFFIX.format(day=day.isoformat()))

    def _next_op_id(self) -> str:
        with self._id_lock:
            WikiOpsLog._counter += 1
            n = WikiOpsLog._counter
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"op_{ts}_{self._pid}_{n}"

    # ---------- write path ----------

    def append(self, user_id: str, op: WikiOp, when: datetime | None = None) -> None:
        when = when or datetime.now(timezone.utc)
        if not op.created_at:
            op.created_at = when.isoformat()
        line = json.dumps(op.to_dict(), ensure_ascii=False)
        slot = (user_id, when.date().isoformat())

        if self.mode == "sync":
            self._write_segment(user_id, when.date(), [line])
            return

        with self._lock:
            self._buf.setdefault(slot, []).append(line)
            self._mark_dirty()

    def _write_segment(self, user_id: str, day: date, lines: list[str]) -> None:
        # Segment names must not collide across processes or across
        # flushes within the same microsecond.
        suffix = f"{datetime.now(timezone.utc).strftime('%H%M%S%f')}-{self._pid}-{self._next_seq()}"
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        self.backend.put_bytes(self._segment_key(user_id, day, suffix), payload)

    def _next_seq(self) -> int:
        with self._id_lock:
            WikiOpsLog._counter += 1
            return WikiOpsLog._counter

    def _flush_locked(self) -> None:
        for (user_id, day_iso), lines in list(self._buf.items()):
            if not lines:
                continue
            self._write_segment(user_id, date.fromisoformat(day_iso), lines)
        self._buf.clear()

    # ---------- read path ----------

    def read_day(self, user_id: str, day: date) -> list[dict]:
        """Reads all segments for the day plus any legacy single-file log,
        then sorts by created_at so segment write order doesn't matter."""
        self.flush()  # don't hide this process's own un-flushed records from a reader
        records: list[dict] = []

        legacy = self.backend.get_bytes(self._legacy_key(user_id, day))
        if legacy is not None:
            records.extend(
                json.loads(line) for line in legacy.data.decode("utf-8").splitlines() if line.strip()
            )

        prefix = wiki_key(user_id, f"_ops/{day.isoformat()}") + "/"
        for key in self.backend.list_keys(prefix):
            if not key.endswith(".jsonl"):
                continue
            raw = self.backend.get_bytes(key)
            if raw is None:
                continue
            records.extend(
                json.loads(line) for line in raw.data.decode("utf-8").splitlines() if line.strip()
            )

        records.sort(key=lambda r: (r.get("created_at", ""), r.get("op_id", "")))
        return records

    def compact_day(self, user_id: str, day: date) -> int:
        """Fold a day's segments into one object and delete them. Run this
        for closed days on a schedule — segments keep writes cheap, but an
        uncompacted busy day turns read_day() into thousands of small GETs.
        Returns the number of records in the compacted file."""
        records = self.read_day(user_id, day)
        if not records:
            return 0
        payload = ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")
        self.backend.put_bytes(self._legacy_key(user_id, day), payload)
        for key in self.backend.list_keys(wiki_key(user_id, f"_ops/{day.isoformat()}") + "/"):
            if key.endswith(".jsonl"):
                self.backend.delete(key)
        return len(records)
