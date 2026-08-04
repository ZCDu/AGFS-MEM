"""
Session log tests.

Two properties matter: a conversation is retrievable by identity, and a stored
fact can be traced back to the conversation that produced it.
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta

import pytest

from app.rawlog.sessions import (SessionIdError, SessionLog, evidence_ref,
                                 new_session_id, parse_evidence_ref,
                                 validate_session_id)
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def log():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield SessionLog(backend)
    backend.close()


def test_a_session_is_one_file_not_one_per_turn(log):
    """The reason for per-session sharding: appending to a day file means the
    Nth message rewrites the N-1 before it, which is quadratic in messages per
    day. A session file is bounded by one conversation."""
    sid = new_session_id()
    log.append("u", sid, [{"role": "user", "content": "first"}])
    log.append("u", sid, [{"role": "assistant", "content": "second"}])
    result = log.append("u", sid, [{"role": "user", "content": "third"}])

    assert result["total"] == 3
    # Sessions live under year/month now, so list from the root prefix.
    keys = [k for k in log.backend.list_keys("u/sessions/") if k.endswith(".jsonl")]
    assert len(keys) == 1


def test_session_ids_cannot_escape_their_prefix(log):
    """Ids go straight into an object key. Rejecting is safer than sanitising:
    a silently rewritten id means the evidence pointer stops resolving."""
    for bad in ("../../etc/passwd", "a/b", "", "x" * 80, "with space"):
        with pytest.raises(SessionIdError):
            validate_session_id(bad)
    validate_session_id("072753-b0024654")


def test_transcript_is_the_shape_the_extractor_accepts(log):
    """So a stored session can be re-extracted without the browser holding the
    conversation."""
    sid = new_session_id()
    log.append("u", sid, [{"role": "user", "content": "Who leads Orion?"},
                          {"role": "assistant", "content": "Alice Chen does."}])
    text = log.transcript("u", sid)
    assert "User: Who leads Orion?" in text
    assert "Assistant: Alice Chen does." in text


def test_list_day_summarises_without_returning_whole_conversations(log):
    a, b = new_session_id(), new_session_id()
    log.append("u", a, [{"role": "user", "content": "first conversation"}])
    log.append("u", b, [{"role": "user", "content": "second conversation"}])

    rows = log.list_day("u", date.today())
    assert {r["session_id"] for r in rows} == {a, b}
    assert all("preview" in r and "messages" in r for r in rows)


def test_reading_an_absent_session_is_empty_not_an_error(log):
    assert log.read("u", "nosuchsession", day=date.today()) == []
    assert log.list_day("u", date.today() - timedelta(days=5)) == []


def test_one_corrupt_line_does_not_lose_the_conversation(log):
    sid = new_session_id()
    log.append("u", sid, [{"role": "user", "content": "keep me"}])
    key = log._key("u", date.today(), sid)
    raw = log.backend.get_bytes(key).data.decode()
    log.backend.put_bytes(key, (raw + "{ not json\n" +
                                '{"role":"user","content":"keep me too"}\n').encode())

    contents = [m["content"] for m in log.read("u", sid, day=date.today())]
    assert contents == ["keep me", "keep me too"]


# ---------- evidence ----------

def test_evidence_ref_round_trips_and_carries_the_date(log):
    """The date is included so resolving a pointer is one read, rather than
    searching backwards through daily listings."""
    ref = evidence_ref(date(2026, 8, 3), "072753-b0024654")
    assert ref == "session:2026-08-03:072753-b0024654"
    assert parse_evidence_ref(ref) == (date(2026, 8, 3), "072753-b0024654")


def test_non_session_evidence_is_left_alone(log):
    """`evidence` also holds free-form strings from other sources."""
    assert parse_evidence_ref("orion-design-doc") is None
    assert parse_evidence_ref("session:not-a-date:x") is None
    assert parse_evidence_ref("session:2026-08-03:../escape") is None


def test_a_stored_fact_traces_back_to_its_conversation(log, monkeypatch):
    """The whole point: "why do you believe this?" answered by finding the fact
    through the graph at no storage cost, then one targeted read."""
    import json

    from app.extract.extractor import ConversationExtractor
    from app.extract.llm import LLMClient
    from app.graph.store import EntityGraphStore

    sid = new_session_id()
    log.append("u", sid, [{"role": "user", "content": "Alice Chen leads retrieval."}])

    class Stub(LLMClient):
        configured = True

        def __init__(self):
            super().__init__(api_key="k", base_url="http://x", model="m")

        def complete(self, system, user, **kw):
            return json.dumps({"entities": [{
                "title": "Alice Chen", "type": "person", "summary": "Leads retrieval.",
                "facts": [{"text": "Leads the retrieval workstream.", "confidence": 0.9}]}],
                "relations": [], "discarded": []})

    store = EntityGraphStore(log.backend)
    ref = evidence_ref(date.today(), sid)
    extractor = ConversationExtractor(store, Stub())
    plan = extractor.plan("u", "We decided today that Alice Chen leads the "
                               "retrieval workstream, deadline 2026-08-15.",
                          evidence=ref)
    extractor.apply("u", plan)
    store.flush()

    fact = store.get_entity("u", "person/alice-chen", touch=False).facts[0]
    assert fact.evidence == [ref], "apply must forward evidence, not just plan it"

    day, found = parse_evidence_ref(fact.evidence[0])
    assert "Alice Chen leads retrieval." in log.transcript("u", found, day=day)


# ---------- endpoints ----------

def test_chat_logs_both_turns_and_returns_the_session(client, monkeypatch):
    """Without this the conversation exists only in the browser: close the tab
    and it is gone, and re-extraction with an improved prompt is impossible."""
    import app.api.routes_chat as rc

    class FakeLLM:
        configured = True
        model = "fake-model"

        def complete(self, system, user, **kw):
            return "Alice Chen leads the retrieval team."

    monkeypatch.setattr(rc, "_client", lambda: FakeLLM())

    r = client.post("/v1/users/demo/chat",
                    json={"messages": [{"role": "user", "content": "who leads retrieval?"}]})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert sid

    stored = client.get(f"/v1/users/demo/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "who leads retrieval?"
    # The journal records what shaped the reply, not just the messages.
    assert stored[1]["model"] == "fake-model"


def test_session_endpoints_round_trip(client):
    r = client.post("/v1/users/demo/sessions",
                    json={"messages": [{"role": "user", "content": "hello there"}]})
    assert r.status_code == 200
    sid = r.json()["session_id"]

    client.post("/v1/users/demo/sessions",
                json={"session_id": sid,
                      "messages": [{"role": "assistant", "content": "hi back"}]})

    assert len(client.get(f"/v1/users/demo/sessions/{sid}").json()["messages"]) == 2
    assert any(s["session_id"] == sid
               for s in client.get("/v1/users/demo/sessions").json())
    assert client.get("/v1/users/demo/sessions/nope").status_code == 404


# ---------- raw files ----------

def test_uploaded_bytes_are_stored_unchanged(client):
    """The archival copy. Text extraction is lossy, and only keeping the
    extracted form would mean later improvements — or a PDF reader, which does
    not exist yet — could never be applied to files already uploaded."""
    original = b"# Orion\n\nAlice Chen leads retrieval.\n\x01\x02"
    r = client.post("/v1/users/demo/files",
                    files={"file": ("notes.md", original, "text/markdown")})
    assert r.status_code == 200
    file_id = r.json()["file_id"]

    got = client.get(f"/v1/users/demo/files/{file_id}/content")
    assert got.content == original


def test_binary_files_are_stored_but_reported_as_unreadable(client):
    """A model handed decoded binary invents content confidently. Being told
    the file is unreadable is strictly better."""
    r = client.post("/v1/users/demo/files",
                    files={"file": ("report.pdf", b"%PDF-1.7\x00\x00junk",
                                    "application/pdf")})
    meta = r.json()
    assert meta["text_extractable"] is False
    assert "PDF" in meta["note"]
    # Stored regardless, so a PDF reader added later can use it.
    assert client.get(f"/v1/users/demo/files/{meta['file_id']}/content").status_code == 200


def test_filenames_never_become_storage_keys(client):
    """A hostile or awkward filename must not escape its prefix, collide, or
    overwrite anything. The key uses a generated id; the name is display only."""
    r = client.post("/v1/users/demo/files",
                    files={"file": ("../../etc/passwd", b"harmless", "text/plain")})
    meta = r.json()
    assert "/" not in meta["file_id"]
    assert client.get(f"/v1/users/demo/files/{meta['file_id']}/content").content == b"harmless"


def test_empty_upload_is_rejected(client):
    r = client.post("/v1/users/demo/files",
                    files={"file": ("empty.txt", b"", "text/plain")})
    assert r.status_code == 422


def test_chat_reads_an_attached_file_and_journals_it(client, monkeypatch):
    """The whole point of attachments, plus the journal recording WHAT the
    model was given — messages alone do not explain a surprising reply."""
    import app.api.routes_chat as rc

    seen = {}

    class FakeLLM:
        configured = True
        model = "fake-model"

        def complete(self, system, user, **kw):
            seen["system"] = system
            return "The deadline is 2026-08-15."

    monkeypatch.setattr(rc, "_client", lambda: FakeLLM())

    up = client.post("/v1/users/demo/files",
                     files={"file": ("notes.md",
                                     b"Orion deadline is 2026-08-15.", "text/markdown")})
    file_id = up.json()["file_id"]

    r = client.post("/v1/users/demo/chat", json={
        "messages": [{"role": "user", "content": "what is the deadline?"}],
        "attachments": [file_id],
    })
    assert r.status_code == 200
    assert "Orion deadline is 2026-08-15." in seen["system"], \
        "the file's text must reach the model"

    stored = client.get(
        f"/v1/users/demo/sessions/{r.json()['session_id']}").json()["messages"]
    assert stored[0]["attachments"], "the journal must record which files were used"
    assert stored[1]["model"] == "fake-model"


def test_an_unreadable_attachment_is_declared_to_the_model(client, monkeypatch):
    """Passing nothing silently would let the model answer as though it had
    read the file."""
    import app.api.routes_chat as rc

    seen = {}

    class FakeLLM:
        configured = True
        model = "m"

        def complete(self, system, user, **kw):
            seen["system"] = system
            return "I could not read it."

    monkeypatch.setattr(rc, "_client", lambda: FakeLLM())

    up = client.post("/v1/users/demo/files",
                     files={"file": ("scan.pdf", b"%PDF-1.7\x00binary",
                                     "application/pdf")})
    client.post("/v1/users/demo/chat", json={
        "messages": [{"role": "user", "content": "what does it say?"}],
        "attachments": [up.json()["file_id"]],
    })
    assert "cannot be read" in seen["system"]


def test_a_missing_attachment_does_not_fail_the_turn(client, monkeypatch):
    import app.api.routes_chat as rc

    class FakeLLM:
        configured = True
        model = "m"

        def complete(self, system, user, **kw):
            return "ok"

    monkeypatch.setattr(rc, "_client", lambda: FakeLLM())
    r = client.post("/v1/users/demo/chat", json={
        "messages": [{"role": "user", "content": "hello"}],
        "attachments": ["999999-abcdef"],
    })
    assert r.status_code == 200


def test_sessions_are_filed_under_year_and_month(log):
    """A flat day-level layout puts every day of every year in one listing, so
    "what happened last March" means scanning the whole history."""
    from datetime import datetime, timezone

    when = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)
    sid = new_session_id(when)
    log.append("u", sid, [{"role": "user", "content": "hello"}], when=when)

    keys = [k for k in log.backend.list_keys("u/sessions/") if k.endswith(".jsonl")]
    assert keys == [f"u/sessions/2026/03/2026-03-04/{sid}.jsonl"]


def test_a_month_is_one_listing_not_thirty_one(log):
    from datetime import datetime, timezone

    for day in (4, 19, 27):
        when = datetime(2026, 3, day, tzinfo=timezone.utc)
        log.append("u", new_session_id(when), [{"role": "user", "content": "x"}],
                   when=when)

    rows = log.list_month("u", 2026, 3)
    assert len(rows) == 3
    assert {r["date"] for r in rows} == {"2026-03-04", "2026-03-19", "2026-03-27"}


def test_a_range_spanning_months_returns_each_session_once(log):
    """list_range lists whole months in one call and walks only the partial
    months at each end — so it must not double-count the overlap."""
    from datetime import date, datetime, timezone

    for month, day in ((3, 4), (4, 15), (8, 3)):
        when = datetime(2026, month, day, tzinfo=timezone.utc)
        log.append("u", new_session_id(when), [{"role": "user", "content": "x"}],
                   when=when)

    rows = log.list_range("u", date(2026, 3, 1), date(2026, 8, 31))
    assert [r["date"] for r in rows] == ["2026-03-04", "2026-04-15", "2026-08-03"]
    assert len(rows) == len({(r["date"], r["session_id"]) for r in rows})
