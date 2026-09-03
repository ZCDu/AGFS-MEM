"""Medium-term memory notes ("stickers").

The middle tier: cheap to write (no LLM, no routing), lives by tags, expires.
What is pinned here:
  - create / list / get / delete via the API
  - tag filtering and active (non-expired) filtering
  - expiry: an expired note surfaces in /expired but is hidden from active
  - a note is cheap to write — no extractor, no wiki created
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.rawlog.notes import NotesLog
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield backend
    backend.close()


def test_put_get_list_and_delete(store):
    notes = NotesLog(store)
    created = notes.put("alice", {"text": "Sofia starts 2026-11-02",
                                  "tags": ["hiring"]})
    assert created["id"].startswith("note-")

    got = notes.get("alice", created["id"])
    assert got["text"] == "Sofia starts 2026-11-02"
    assert got["tags"] == ["hiring"]

    listed = notes.list("alice")
    assert len(listed) == 1

    assert notes.delete("alice", created["id"]) is True
    assert notes.list("alice") == []


def test_tag_filter(store):
    notes = NotesLog(store)
    notes.put("alice", {"text": "hiring one", "tags": ["hiring"]})
    notes.put("alice", {"text": "infra one", "tags": ["infra"]})

    assert {n["text"] for n in notes.list("alice", tag="hiring")} == {"hiring one"}
    assert {n["text"] for n in notes.list("alice", tag="infra")} == {"infra one"}
    assert len(notes.list("alice")) == 2


def test_expiry_surfaces_in_expired_not_in_active(store):
    notes = NotesLog(store)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    notes.put("alice", {"text": "already due", "tags": ["todo"], "expires_at": past})
    notes.put("alice", {"text": "still valid", "tags": ["todo"], "expires_at": future})
    notes.put("alice", {"text": "never expires", "tags": ["keep"]})

    # active hides the due one.
    active_texts = {n["text"] for n in notes.list("alice", active=True)}
    assert active_texts == {"still valid", "never expires"}

    # the due one surfaces under /expired.
    status = notes.expire_status("alice")
    assert status["expired"] == 1
    assert status["expired_notes"][0]["text"] == "already due"
