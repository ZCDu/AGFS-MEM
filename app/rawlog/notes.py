"""Medium-term memory notes ("stickers").

Where short-term is the working memory (recent context, handled by the chat /
agent logic) and long-term is the knowledge-graph wiki (entities, facts,
routes), this is the MIDDLE tier: a small, self-contained note that a user (or
an agent) can drop instantly without the cost of LLM extraction or graph
routing.

WHAT MAKES A NOTE DIFFERENT FROM A WIKI ENTITY
    - Cheap to write: a single PUT. No assessor gate, no LLM naming, no router
      decision, no wiki materialisation.
    - Tag/label scoped, not graph-home scoped: a note lives by its tags; you
      query it by tag.
    - Expiring: a note has an optional `expires_at`. That is the whole point
      of "medium" — a follow-up, a temporary decision, a reminder. It should
      surface at expiry (so it is not forgotten) and can then be re-filed,
      promoted to the wiki, or deleted. It is NOT permanent wiki knowledge.
    - Source-linked: each note carries the session/evidence it came from, so
      it can be promoted to the graph later with provenance.

STORAGE
    One JSON object per note: {user_id}/notes/{note_id}.json — not day-sharded,
    because notes are mutable (edited, deleted, expired) rather than append-
    only. Listing a user's notes is one prefix scan; tag filtering happens in
    memory after the read. A note_id is a random hex so it can be created and
    deleted without coordination.
"""

from __future__ import annotations

import json
import secrets
import logging
from datetime import date, datetime, timezone

from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.notes")

_NOTE_ID = "note-{hex}"


def new_note_id() -> str:
    return f"note-{secrets.token_hex(8)}"


class NotesLog:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    def _prefix(self, user_id: str) -> str:
        return f"{user_id}/notes/"

    def _key(self, user_id: str, note_id: str) -> str:
        return f"{self._prefix(user_id)}{note_id}.json"

    # ---------- writes ----------

    def put(self, user_id: str, note: dict) -> dict:
        """Create or overwrite a note. Normalises fields and stamps a fresh
        created/updated time. `note_id` is generated if absent."""
        note_id = str(note.get("id") or new_note_id())
        record = {
            "id": note_id,
            "text": str(note.get("text") or "").strip(),
            "tags": [str(t).strip() for t in (note.get("tags") or []) if t and str(t).strip()],
            "created_at": note.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "expires_at": note.get("expires_at"),   # ISO string or None
            "source": note.get("source"),           # e.g. "session:2026-08-26:abc"
        }
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.backend.put_bytes(self._key(user_id, note_id),
                               json.dumps(record, ensure_ascii=False).encode("utf-8"))
        return record

    # ---------- reads ----------

    def get(self, user_id: str, note_id: str) -> dict | None:
        raw = self.backend.get_bytes(self._key(user_id, note_id))
        if raw is None:
            return None
        try:
            return json.loads(raw.data.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("note %s for %s is unreadable", note_id, user_id)
            return None

    def list(self, user_id: str, tag: str | None = None, active: bool = False,
             include_expired: bool = True) -> list[dict]:
        """All of a user's notes, optionally filtered.

        `tag`      -> keep only notes carrying that tag.
        `active`   -> keep only non-expired notes (no expires_at, or not yet due).
        `include_expired` -> when False, drop expired notes even if `active` is
                             False. (active=True implies include_expired=False.)
        Sorted newest-first by updated_at.
        """
        now = datetime.now(timezone.utc)
        out = []
        for key in self.backend.list_keys(self._prefix(user_id)):
            if not key.endswith(".json"):
                continue
            note = self.get(user_id, key.rsplit("/", 1)[-1][:-len(".json")])
            if not note:
                continue
            if tag and tag not in note.get("tags", []):
                continue
            expired = self._is_expired(note, now)
            if active and expired:
                continue
            if not include_expired and expired:
                continue
            out.append(note)
        return sorted(out, key=lambda n: n.get("updated_at", ""), reverse=True)

    def expire_status(self, user_id: str) -> dict:
        """Snapshot for the medium-memory dashboard: active vs expired counts,
        plus the expired notes (which is what a sweep would re-surface)."""
        all_notes = self.list(user_id)
        now = datetime.now(timezone.utc)
        active = [n for n in all_notes if not self._is_expired(n, now)]
        expired = [n for n in all_notes if self._is_expired(n, now)]
        return {"active": len(active), "expired": len(expired),
                "notes": all_notes, "expired_notes": expired}

    # ---------- delete ----------

    def delete(self, user_id: str, note_id: str) -> bool:
        """Delete a note. Returns whether anything was removed."""
        key = self._key(user_id, note_id)
        exists = self.backend.get_bytes(key) is not None
        if exists:
            self.backend.delete(key)
        return exists

    # ---------- helpers ----------

    @staticmethod
    def _is_expired(note: dict, now: datetime | None = None) -> bool:
        raw = note.get("expires_at")
        if not raw:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(str(raw))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp <= now
        except ValueError:
            return False
