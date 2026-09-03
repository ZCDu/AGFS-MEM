"""
Conversation sessions, stored one JSONL file per session.

    {user_id}/sessions/{YYYY}/{MM}/{YYYY-MM-DD}/{session_id}.jsonl

WHY PER-SESSION RATHER THAN PER-DAY
    The day-sharded raw-facts log (app/rawlog/log.py) has two problems for
    conversations.

    Cost: object stores have no append, so adding a line means re-uploading the
    whole object. With one file per day, the 50th message of the day rewrites
    the 49 before it — quadratic in messages per day. A session file is
    naturally bounded by the length of one conversation, so the rewrite stays
    small however long you keep using the system.

    Grain: a day file cannot answer "show me that conversation". A session can,
    which is what makes it usable as an evidence pointer.

EVIDENCE
    Facts already carry an `evidence` list. Recording the session id there
    turns "where did this claim come from?" from a matter of trust into one
    targeted GET: find the fact through the graph (no storage reads, the index
    is in memory), then fetch exactly the conversation it came from.

    That is the reason a day file is not good enough as a pointer. It also
    makes re-extraction possible: when the prompt or schema improves, the
    original transcripts are still there to run again.

WHAT THIS IS NOT
    Not a search index. Finding "when did we discuss Rokossovsky" by scanning
    sessions is O(total history) and grows without bound. That question belongs
    to the graph, which answers it from memory in constant time. This layer
    exists for audit and reprocessing — retrieval by identity, not by content.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.sessions")

# Session ids go straight into an object key, so constrain them rather than
# trusting a client. Rejecting is safer than sanitising: a silently rewritten
# id means the caller's evidence pointer no longer resolves.
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAX_MESSAGE_CHARS = 100_000


class SessionIdError(ValueError):
    pass


def new_session_id(when: datetime | None = None) -> str:
    """Sortable by creation time, with enough randomness to avoid collisions
    between concurrent clients."""
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime('%H%M%S')}-{secrets.token_hex(4)}"


def validate_session_id(session_id: str) -> str:
    if not _SESSION_ID.match(session_id or ""):
        raise SessionIdError(
            f"Invalid session id {session_id!r}: use letters, digits, dot, dash "
            f"or underscore, 1-64 characters.")
    return session_id


class SessionLog:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    # ---------- keys ----------

    def _day_prefix(self, user_id: str, day: date) -> str:
        """Year and month directories above the day.

        A flat day-level layout puts every day of every year in one listing,
        so "what happened last March" means scanning the whole history. With
        year/month, a month is one prefix and a year is one listing of twelve.
        The full date is repeated in the leaf so a path is readable on its own
        without reassembling it from three parent directories.
        """
        return f"{user_id}/sessions/{day.year:04d}/{day.month:02d}/{day.isoformat()}/"

    def _key(self, user_id: str, day: date, session_id: str) -> str:
        return f"{self._day_prefix(user_id, day)}{session_id}.jsonl"

    def month_prefix(self, user_id: str, year: int, month: int) -> str:
        return f"{user_id}/sessions/{year:04d}/{month:02d}/"

    def year_prefix(self, user_id: str, year: int) -> str:
        return f"{user_id}/sessions/{year:04d}/"

    # ---------- writing ----------

    def append(self, user_id: str, session_id: str, messages: list[dict],
               when: datetime | None = None) -> dict:
        """Append messages to a session. Returns a summary of the result.

        Read-modify-write against one session file. No conditional write: two
        writers appending to the same session would be the same person in two
        tabs, and losing a turn there is less bad than failing the request.
        The graph, where correctness matters, does use conditional writes.
        """
        validate_session_id(session_id)
        when = when or datetime.now(timezone.utc)
        day = when.date()
        key = self._key(user_id, day, session_id)

        prepared = []
        for m in messages:
            record = dict(m)
            record.setdefault("ts", when.isoformat())
            content = str(record.get("content", ""))
            if len(content) > MAX_MESSAGE_CHARS:
                record["content"] = content[:MAX_MESSAGE_CHARS]
                record["truncated"] = True
            prepared.append(record)

        current = self.backend.get_bytes(key)
        existing = current.data.decode("utf-8").splitlines() if current else []
        lines = existing + [json.dumps(r, ensure_ascii=False) for r in prepared]
        self.backend.put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))

        return {"session_id": session_id, "date": day.isoformat(),
                "appended": len(prepared), "total": len(lines)}

    def _find_key(self, user_id: str, session_id: str,
                  day: date | None) -> tuple[str | None, list[dict] | None]:
        """Resolve a session to (key, parsed messages), locating it backwards
        when the date is not known. Returns (None, None) when the session does
        not exist, so a best-effort mark cannot raise."""
        validate_session_id(session_id)
        if day is not None:
            key = self._key(user_id, day, session_id)
            raw = self.backend.get_bytes(key)
            if raw is None:
                return None, None
            return key, self._parse(raw)
        key = self.find(user_id, session_id)
        if key is None:
            return None, None
        return key, self._parse(self.backend.get_bytes(key))

    # ---------- reading ----------

    def read(self, user_id: str, session_id: str, day: date | None = None) -> list[dict]:
        """One session's messages.

        `day` makes it a single GET. Without it the session has to be located
        first, which is why the id is returned alongside its date everywhere.
        """
        validate_session_id(session_id)
        if day is not None:
            raw = self.backend.get_bytes(self._key(user_id, day, session_id))
            return self._parse(raw)

        found = self.find(user_id, session_id)
        if found is None:
            return []
        return self._parse(self.backend.get_bytes(found))

    # ---------- examined-state (shared with the review + autocapture paths) ----------
    #
    # A session is a stream of messages; only the parts not yet EXAMINED should
    # ever be passed to the extractor/apply pipeline. "Examined" means the
    # message has already been reviewed and applied (interactive flow) or
    # captured by the timer. Tracking this per message -- not per whole session
    # -- keeps two paths from trampling the same reviewed data:
    #   * a human reviews a conversation and applies it, then the timer must
    #     NOT re-examine those same messages;
    #   * a session that grows after being captured only feeds its NEW,
    #     unexamined messages to the next run.
    #
    # The flag lives on the message record itself (so it travels with the data),
    # written back as one file rewrite per mark call. A single capture run marks
    # once (after its messages are processed), not per message, so the rewrite
    # cost is bounded by one session file per run.

    def read_unexamined(self, user_id: str, session_id: str,
                        day: date | None = None) -> list[tuple[int, dict]]:
        """(index, message) pairs for the parts of a session that have not yet
        been examined (no `examined` flag). `index` is the message's position in
        the session file, which is what `mark_examined` accepts. The whole
        session is read so indexes stay stable even when earlier messages were
        already examined."""
        messages = self.read(user_id, session_id, day=day)
        return [(i, m) for i, m in enumerate(messages) if not m.get("examined")]

    def mark_examined(self, user_id: str, session_id: str, indexes: list[int],
                      day: date | None = None, by: str = "autocapture") -> int:
        """Flag the messages at `indexes` (0-based positions in the session
        file) as examined, recording who/what examined them. Rewrites the file
        once. Returns the number of messages newly marked (already-marked ones
        are not double-counted)."""
        if not indexes:
            return 0
        key, messages = self._find_key(user_id, session_id, day)
        if messages is None:
            return 0
        wanted = set(indexes)
        changed = 0
        for i, m in enumerate(messages):
            if i in wanted and not m.get("examined"):
                m["examined"] = True
                m["examined_at"] = datetime.now(timezone.utc).isoformat()
                m["examined_by"] = by
                changed += 1
        if not changed:
            return 0
        lines = [json.dumps(r, ensure_ascii=False) for r in messages]
        self.backend.put_bytes(key, ("\n".join(lines) + "\n").encode("utf-8"))
        return changed

    def find(self, user_id: str, session_id: str, search_days: int = 400) -> str | None:
        """Locate a session's key without knowing its date.

        Walks backwards from today. Costs one listing per day, so callers that
        know the date should pass it — which is why evidence pointers record
        `date:session_id` rather than the bare id.
        """
        validate_session_id(session_id)
        today = datetime.now(timezone.utc).date()
        for offset in range(search_days):
            day = today - timedelta(days=offset)
            key = self._key(user_id, day, session_id)
            if self.backend.get_bytes(key) is not None:
                return key
        return None

    def delete(self, user_id: str, session_id: str, day: date | None = None) -> bool:
        """Delete one session log. Returns whether anything was removed.

        `day` makes it a single targeted delete. Without it the session is
        located first (backwards search), then removed.
        """
        validate_session_id(session_id)
        key = self._key(user_id, day, session_id) if day is not None else None
        if key is None:
            key = self.find(user_id, session_id)
        if key is None:
            return False
        self.backend.delete(key)
        return True

    def _summarise(self, key: str) -> dict | None:
        records = self._parse(self.backend.get_bytes(key))
        if not records:
            return None
        parts = key.split("/")
        return {
            "session_id": parts[-1][:-len(".jsonl")],
            "date": parts[-2],
            "messages": len(records),
            "started_at": records[0].get("ts", ""),
            "ended_at": records[-1].get("ts", ""),
            "preview": str(records[0].get("content", ""))[:120],
        }

    def list_day(self, user_id: str, day: date) -> list[dict]:
        """Session summaries for one day: id, message count, first/last time."""
        out = []
        for key in self.backend.list_keys(self._day_prefix(user_id, day)):
            if not key.endswith(".jsonl"):
                continue
            summary = self._summarise(key)
            if summary:
                out.append(summary)
        return sorted(out, key=lambda s: s["started_at"])

    def list_month(self, user_id: str, year: int, month: int) -> list[dict]:
        """Every session in a month from ONE listing.

        This is what the year/month layout buys: without it a month means up
        to 31 separate day listings, each a round-trip.
        """
        out = []
        for key in self.backend.list_keys(self.month_prefix(user_id, year, month)):
            if not key.endswith(".jsonl"):
                continue
            summary = self._summarise(key)
            if summary:
                out.append(summary)
        return sorted(out, key=lambda s: s["started_at"])

    def list_range(self, user_id: str, start: date, end: date) -> list[dict]:
        """Whole months are listed in one call; only the partial months at
        each end are walked day by day."""
        out: list[dict] = []
        seen: set[str] = set()
        day = start
        while day <= end:
            month_start = day.replace(day=1)
            next_month = (month_start + timedelta(days=32)).replace(day=1)
            month_end = next_month - timedelta(days=1)
            if month_start >= start and month_end <= end:
                rows = self.list_month(user_id, day.year, day.month)
                day = next_month
            else:
                rows = self.list_day(user_id, day)
                day += timedelta(days=1)
            for r in rows:
                key = f"{r['date']}/{r['session_id']}"
                if key not in seen:
                    seen.add(key)
                    out.append(r)
        return sorted(out, key=lambda s: s["started_at"])

    def transcript(self, user_id: str, session_id: str, day: date | None = None) -> str:
        """The session rendered for re-extraction — the same shape the
        extractor already accepts, so a stored session can be reprocessed
        without the browser holding the conversation."""
        lines = []
        for m in self.read(user_id, session_id, day=day):
            role = "User" if m.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {m.get('content', '')}")
        return "\n\n".join(lines)

    @staticmethod
    def _parse(raw) -> list[dict]:
        if raw is None:
            return []
        out = []
        for line in raw.data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # One bad line must not lose the rest of the conversation.
                logger.warning("skipping unparseable line in a session log")
        return out


def evidence_ref(day: date | str, session_id: str) -> str:
    """The form recorded in a fact's `evidence` list.

    Carries the date as well as the id so resolving it is one GET rather than a
    backwards search through daily listings.
    """
    d = day if isinstance(day, str) else day.isoformat()
    return f"session:{d}:{session_id}"


def parse_evidence_ref(ref: str) -> tuple[date, str] | None:
    """Inverse of evidence_ref. Returns None for anything else, since
    `evidence` also holds free-form strings from other sources."""
    if not ref.startswith("session:"):
        return None
    parts = ref.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        return date.fromisoformat(parts[1]), validate_session_id(parts[2])
    except (ValueError, SessionIdError):
        return None
