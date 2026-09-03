"""
HTTP-level tests for share links (app/api/routes_shares.py).

Uses the shared `client` fixture from tests/conftest.py (AUTH_MODE=off, a
throwaway disk-backed store per test). The chat endpoint needs an LLM, so
those tests stub it via FastAPI's dependency_overrides rather than calling
the real API -- consistent with how tests/test_extract.py pins the
extraction layer with a StubLLM, just wired at the HTTP boundary instead of
constructed directly.
"""

from __future__ import annotations

import json

import pytest

from app.deps import get_extractor
from app.extract.extractor import ConversationExtractor, SYSTEM_PROMPT as EXTRACT_PROMPT
from app.graph.store import EntityGraphStore


@pytest.fixture(autouse=True)
def _no_autocapture_timer(monkeypatch):
    """The `client` fixture's app runs a REAL background AutoCaptureTimer
    thread (AUTO_CAPTURE_ENABLED=true in .env, not overridden by conftest),
    including a startup backfill that touches the same storage backend
    these tests use. Racing a background writer against foreground
    assertions produced exactly the kind of "works alone, fails in the
    suite" flakiness this whole codebase is full of elsewhere -- not a bug
    in the share-link code, just noise this file doesn't need to live with
    since nothing here is testing the timer itself."""
    monkeypatch.setenv("AUTO_CAPTURE_ENABLED", "false")


class StubLLM:
    """Returns a fixed extraction JSON for extractor.plan()'s call (matched
    by its distinctive system prompt) and a fixed plain-text reply for
    everything else (the conversational turn)."""

    configured = True

    def __init__(self, extraction_response: dict, reply_text: str = "Got it, noted."):
        self.extraction_response = json.dumps(extraction_response)
        self.reply_text = reply_text
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, **kw) -> str:
        self.calls.append((system, user))
        if system == EXTRACT_PROMPT:
            return self.extraction_response
        return self.reply_text


def _stub_extractor(client, response: dict, reply_text: str = "Got it, noted."):
    """Override get_extractor for the duration of the test so shared_chat's
    LLM calls are deterministic and need no network. Returns the StubLLM so
    a test can inspect .calls."""
    from app.deps import get_graph_store, get_storage_backend
    llm = StubLLM(response, reply_text)

    def _build():
        store = EntityGraphStore(get_storage_backend())
        return ConversationExtractor(store, llm)

    client.app.dependency_overrides[get_extractor] = _build
    return llm


MEETING_RESPONSE = {
    "entities": [
        {"title": "Dana Lee", "type": "person", "significance": 0.7,
         "summary": "Owns the follow-up on the Q3 planning meeting.",
         "facts": [{"text": "Owns the follow-up action item.", "confidence": 0.9}]},
    ],
    "relations": [
        {"source": "Dana Lee", "target": "Q3 Planning", "category": "related_to",
         "label": "owns_followup", "reason": "stated"},
    ],
    "discarded": [],
}


@pytest.fixture()
def entry(client):
    """An existing entity to share, plus its wiki_id.

    Flushes explicitly: the manifest is a buffered write-behind cache (see
    app/graph/manifest.py), and component_of() reads through it -- without
    a flush, an immediately-following scope computation can see an empty
    manifest even though the entity file itself was already written. Same
    reasoning as the explicit store.flush() calls throughout
    tests/test_merge.py and tests/test_extract.py.
    """
    r = client.put("/v1/users/miguel/wiki", json={
        "type": "event", "title": "Q3 Planning",
        "summary_append": "Quarterly planning meeting.",
    })
    assert r.status_code == 200
    from app.deps import get_storage_backend
    EntityGraphStore(get_storage_backend()).flush()
    return r.json()["wiki_id"]


# ---------- owner: create / list / revoke ----------

def test_create_share_for_existing_entity(client, entry):
    r = client.post(f"/v1/users/miguel/wiki/event/Q3 Planning/share",
                    json={"label": "Q3 Planning"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_user_id"] == "miguel"
    assert body["entry_wiki_id"] == entry
    assert body["active"] is True
    assert body["url"].startswith("/shared/")


def test_create_share_for_missing_entity_404s(client):
    r = client.post("/v1/users/miguel/wiki/event/Nobody Home/share", json={})
    assert r.status_code == 404


def test_list_shares_shows_created_links(client, entry):
    client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={})
    r = client.get("/v1/users/miguel/shares")
    assert r.status_code == 200
    shares = r.json()
    assert len(shares) == 1
    assert shares[0]["entry_wiki_id"] == entry


def test_revoke_share_deactivates(client, entry):
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    r = client.delete(f"/v1/users/miguel/shares/{created['share_id']}")
    assert r.status_code == 204

    # require_share() itself now refuses a revoked link (410) -- there's no
    # "preview a dead link" state to inspect via the guest-facing route.
    preview = client.get(f"/v1/shared/{created['share_id']}")
    assert preview.status_code == 410


def test_revoke_by_a_different_owner_path_404s(client, entry):
    """Revoke is scoped by the URL's user_id matching the link's actual
    owner, independent of whatever AUTH_MODE happens to allow through --
    this is the app's own ownership check, not the auth layer's."""
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    r = client.delete(f"/v1/users/alexei/shares/{created['share_id']}")
    assert r.status_code == 404

    still_active = client.get(f"/v1/shared/{created['share_id']}")
    assert still_active.json()["active"] is True


# ---------- guest: preview ----------

def test_preview_unknown_share_404s(client):
    r = client.get("/v1/shared/totally-made-up-token-value")
    assert r.status_code == 404


def test_preview_shows_scope_size(client, entry):
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    r = client.get(f"/v1/shared/{created['share_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_type"] == "event"
    assert body["scope_size"] == 1  # just the isolated entry entity so far


# ---------- guest: strict chat ----------

def test_shared_chat_applies_in_scope_and_reports_it(client, entry):
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    _stub_extractor(client, MEETING_RESPONSE)

    r = client.post(f"/v1/shared/{created['share_id']}/chat", json={
        "messages": [{"role": "user",
                      "content": "From the Q3 Planning meeting: Dana Lee has agreed to own the follow-up action item and will report back on progress next week."}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"]
    assert body["session_id"]
    applied_ids = {o["wiki_id"] for o in body["applied"]}
    assert "person/dana-lee" in applied_ids
    assert body["rejected"] == []

    # Actually landed on the OWNER's graph.
    dana = client.get("/v1/users/miguel/wiki/person/Dana Lee").json()
    assert any("follow-up" in f["text"] for f in dana["facts"])


def test_shared_chat_rejects_out_of_scope_and_reports_it(client, entry):
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    # An existing, unrelated entity in the owner's graph that the guest has
    # no business touching.
    client.put("/v1/users/miguel/wiki", json={
        "type": "person", "title": "Some Unrelated VP"})
    from app.deps import get_storage_backend
    EntityGraphStore(get_storage_backend()).flush()

    out_of_scope_response = {
        "entities": [{"title": "Some Unrelated VP", "type": "person",
                     "summary": "x", "facts": [{"text": "got a raise", "confidence": 0.9}]}],
        "relations": [], "discarded": [],
    }
    _stub_extractor(client, out_of_scope_response)

    r = client.post(f"/v1/shared/{created['share_id']}/chat", json={
        "messages": [{"role": "user", "content": "Also worth noting from a totally separate conversation: the Unrelated VP got a raise this quarter, unrelated to anything about Q3 Planning."}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == []
    # Both the upsert_entity (touching the existing entity's summary) and
    # the add_fact op the model proposed target the same out-of-scope
    # entity, so both get rejected independently.
    assert len(body["rejected"]) == 2
    assert {o["wiki_id"] for o in body["rejected"]} == {"person/some-unrelated-vp"}

    # Definitely not written.
    vp = client.get("/v1/users/miguel/wiki/person/Some Unrelated VP").json()
    assert not any("raise" in f["text"] for f in vp["facts"])


def test_shared_chat_on_revoked_link_is_rejected(client, entry):
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    client.delete(f"/v1/users/miguel/shares/{created['share_id']}")
    _stub_extractor(client, MEETING_RESPONSE)

    r = client.post(f"/v1/shared/{created['share_id']}/chat",
                    json={"messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 410


def test_shared_chat_marks_session_examined(client, entry):
    """Without this, autocapture would later re-examine the same text with
    NO scope restriction at all -- defeating the whole feature."""
    created = client.post("/v1/users/miguel/wiki/event/Q3 Planning/share", json={}).json()
    _stub_extractor(client, MEETING_RESPONSE)

    r = client.post(f"/v1/shared/{created['share_id']}/chat", json={
        "messages": [{"role": "user",
                      "content": "From the Q3 Planning meeting: Dana Lee has agreed to own the follow-up action item and will report back on progress next week."}],
    })
    session_id = r.json()["session_id"]

    from app.rawlog.sessions import SessionLog
    from app.deps import get_storage_backend
    log = SessionLog(get_storage_backend())
    messages = log.read("miguel", session_id)
    assert messages, "the turn should have been logged under the OWNER, not a guest"
    assert all(m.get("examined") for m in messages)
