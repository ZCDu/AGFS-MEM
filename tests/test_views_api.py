"""
API-level tests for the summarized-views feature (token mode).

Exercises the grant hierarchy, on-demand summarize, admin oversight,
transparency audit, and the diary endpoints through the real HTTP surface.
Uses static tokens: `alice` (owner), `bob` (colleague), and the admin token.
The LLM is faked via routes_views._summarizer_factory.
"""

from __future__ import annotations

import importlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app.config as cfg
from app.wikis.registry import WikiRegistry

TOKEN_ALICE = "tok-alice"
TOKEN_BOB = "tok-bob"
TOKEN_ADMIN = "tok-admin"


class FakeSummarizer:
    """Mimics WikiSummarizer's public surface with an in-memory diary store."""

    def __init__(self, registry, store, llm=None):
        self.registry = registry
        self.store = store
        self.calls = []
        self.diaries = {}
        self.audit = []

    def summarize(self, owner, query="", scope="all", viewer="admin",
                  is_admin=False):
        self.calls.append(("summarize", owner, query, scope, viewer))
        reg = self.registry
        count = 0
        for t in reg.list_topics(owner):
            count += len(self.store.list_entities(
                reg.topic_scope(owner, t.topic_key)))
        return {"summary": f"Summary of {owner}'s wiki.",
                "key_people": [], "decisions": [], "open_items": [],
                "timeline": [], "sources": [],
                "query": query, "scope": scope, "owner": owner,
                "viewer": viewer, "entity_count": count}

    def record_view(self, owner, viewer, scope, kind="summary", note=""):
        self.audit.append({"owner": owner, "viewer": viewer, "kind": kind,
                           "scope": scope, "note": note})

    def list_audit(self, owner, limit=50):
        return [a for a in reversed(self.audit) if a["owner"] == owner][:limit]

    def generate_diary(self, owner, day=None, viewer="system"):
        self.calls.append(("diary", owner, day))
        self.diaries[owner] = {"day": day or "today", "owner": owner,
                               "diary": "Today they worked."}
        return self.diaries[owner]

    def read_diary(self, owner, day=None):
        rec = self.diaries.get(owner)
        return [rec] if rec else []


@pytest.fixture()
def env(monkeypatch):
    root = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", root)
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv(
        "AUTH_TOKENS", f"{TOKEN_ALICE}:alice,{TOKEN_BOB}:bob")
    monkeypatch.setenv("AUTH_ADMIN_TOKEN", TOKEN_ADMIN)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    import app.api.routes_views as rv
    fake = {"summ": None}
    def factory(store):
        if fake["summ"] is None:
            fake["summ"] = FakeSummarizer(WikiRegistry(deps.get_storage_backend()),
                                          store)
        return fake["summ"]
    rv._summarizer_factory = factory
    with TestClient(main.create_app()) as client:
        # Seed home wikis directly (as a first write/lazy ensure would).
        reg = WikiRegistry(deps.get_storage_backend())
        reg.ensure_home("alice")
        reg.ensure_home("bob")
        yield client, fake
    rv._summarizer_factory = None
    deps.get_storage_backend.cache_clear()


def _h(usertoken):
    return {"Authorization": f"Bearer {usertoken}"}


# ---------- grant hierarchy ----------

def test_colleague_cannot_summarize_without_grant(env):
    client, fake = env
    r = client.post("/v1/users/alice/wiki/summarize",
                    json={"query": "what did alice do?"}, headers=_h(TOKEN_BOB))
    assert r.status_code == 403, r.text


def test_owner_can_always_summarize_own(env):
    client, fake = env
    r = client.post("/v1/users/alice/wiki/summarize",
                    json={"query": "my own work"}, headers=_h(TOKEN_ALICE))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner"] == "alice"
    assert body["viewer"] == "alice"


def test_granted_colleague_can_summarize(env):
    client, fake = env
    # alice grants bob a summary view
    r = client.post("/v1/users/alice/wiki/views",
                    json={"viewer": "bob", "scope": "all", "permissions": "summary"},
                    headers=_h(TOKEN_ALICE))
    assert r.status_code == 200, r.text
    # now bob can summarize
    r = client.post("/v1/users/alice/wiki/summarize",
                    json={"query": "alice's snowflake work"},
                    headers=_h(TOKEN_BOB))
    assert r.status_code == 200, r.text
    assert r.json()["viewer"] == "bob"


def test_only_owner_can_grant_views(env):
    client, fake = env
    # bob (not the owner) tries to grant a view of alice's wiki -> 403
    r = client.post("/v1/users/alice/wiki/views",
                    json={"viewer": "carol", "scope": "all"},
                    headers=_h(TOKEN_BOB))
    assert r.status_code == 403, r.text


def test_admin_oversight_summarizes_any_user(env):
    client, fake = env
    r = client.post("/v1/admin/users/alice/wiki/summarize",
                    json={"query": "weekly check"}, headers=_h(TOKEN_ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner"] == "alice"
    assert body["viewer"] == "admin"


# ---------- transparency ----------

def test_summarize_writes_transparent_audit(env):
    client, fake = env
    client.post("/v1/admin/users/alice/wiki/summarize",
                json={"query": "oversight"}, headers=_h(TOKEN_ADMIN))
    # the owner can read the audit trail
    r = client.get("/v1/users/alice/wiki/views/audit",
                   headers=_h(TOKEN_ALICE))
    assert r.status_code == 200, r.text
    views = r.json()["views"]
    assert len(views) == 1
    assert views[0]["viewer"] == "admin"
    assert views[0]["kind"] == "admin-summary"


def test_audit_visible_only_to_owner_or_admin(env):
    client, fake = env
    # bob (neither owner nor admin) cannot see alice's audit trail
    r = client.get("/v1/users/alice/wiki/views/audit", headers=_h(TOKEN_BOB))
    assert r.status_code == 403, r.text


# ---------- diary ----------

def test_diary_generate_and_read(env):
    client, fake = env
    r = client.post("/v1/admin/users/alice/wiki/diary/generate?day=2026-08-31",
                    headers=_h(TOKEN_ADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["generated"] is True

    # the owner reads their own diary
    r = client.get("/v1/users/alice/wiki/diary", headers=_h(TOKEN_ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["owner"] == "alice"
    assert len(r.json()["entries"]) == 1


def test_diary_admin_reads_employee(env):
    client, fake = env
    client.post("/v1/admin/users/alice/wiki/diary/generate",
                headers=_h(TOKEN_ADMIN))
    r = client.get("/v1/users/alice/wiki/diary", headers=_h(TOKEN_ADMIN))
    assert r.status_code == 200, r.text
    assert len(r.json()["entries"]) == 1


def test_diary_colleague_cannot_read(env):
    client, fake = env
    # bob has no right to alice's diary
    r = client.get("/v1/users/alice/wiki/diary", headers=_h(TOKEN_BOB))
    assert r.status_code in (403, 404), r.text


def test_diary_generate_requires_admin(env):
    client, fake = env
    # alice (not an admin) cannot force diary generation
    r = client.post("/v1/admin/users/alice/wiki/diary/generate",
                    headers=_h(TOKEN_ALICE))
    assert r.status_code == 403, r.text
