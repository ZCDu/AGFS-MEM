"""
Collaborative wiki entity CRUD (routes_wiki_entities).

Covers the new wiki-scoped endpoints at /v1/wikis/{wiki_id}/entities/... and
their grant enforcement. The happy-path tests run under AUTH_MODE=off, so the
"caller" is the platform admin credential (is_admin=True), which legitimately
bypasses per-wiki grants — that is the right way to exercise the read/write
mechanics. Grant enforcement itself is unit-tested against _grant directly
with fabricated cred dicts, because AUTH_MODE=off can never produce a
non-admin caller.
"""

from __future__ import annotations

import pytest

from app.api.routes_wiki_entities import _grant
from app.wikis.registry import ROLE_ADMIN, ROLE_READ, ROLE_WRITE


# ---------- happy path: wiki-scoped entity CRUD (AUTH_MODE=off, admin) ----------

def _make_wiki(client, title="Shared Project", wiki_id=None):
    body = {"title": title}
    if wiki_id:
        body["wiki_id"] = wiki_id
    r = client.post("/v1/wikis", json=body)
    assert r.status_code == 201, r.text
    return r.json()["wiki_id"]


def test_wiki_scoped_upsert_and_list(client):
    wid = _make_wiki(client, title="CI Runner Migration", wiki_id="ci-runner-migration")

    # Upsert an entity into the shared wiki via the wiki-scoped path.
    r = client.put(f"/v1/wikis/{wid}/entities", json={
        "type": "person", "title": "Alice Chen", "summary_append": "Engineer.",
        "aliases": ["Alice"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wiki_id"] == "person/alice-chen"

    # List the wiki's entities.
    r = client.get(f"/v1/wikis/{wid}/entities")
    assert r.status_code == 200, r.text
    titles = [e["title"] for e in r.json()]
    assert "Alice Chen" in titles


def test_wiki_scoped_list_matches_description(client):
    """`q` on the wiki-scoped catalogue must also match against an entity's
    description (compact), not just its title/aliases/wiki_id."""
    wid = _make_wiki(client, title="Launch Ops", wiki_id="launch-ops")
    client.put(f"/v1/wikis/{wid}/entities", json={
        "type": "project", "title": "Orion",
        "compact": "Quarterly launch timeline for the propulsion team.",
    })
    r = client.get(f"/v1/wikis/{wid}/entities", params={"q": "propulsion timeline"})
    assert r.status_code == 200, r.text
    assert {e["wiki_id"] for e in r.json()} == {"project/orion"}


def test_wiki_scoped_list_title_match_outranks_description_match(client):
    wid = _make_wiki(client, title="Launch Ops 2", wiki_id="launch-ops-2")
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "project", "title": "Timeline Overhaul"})
    client.put(f"/v1/wikis/{wid}/entities", json={
        "type": "project", "title": "Orion",
        "compact": "Includes a full timeline for the propulsion team.",
    })
    r = client.get(f"/v1/wikis/{wid}/entities", params={"q": "timeline"})
    assert r.status_code == 200, r.text
    ids = [e["wiki_id"] for e in r.json()]
    assert ids == ["project/timeline-overhaul", "project/orion"]


def test_wiki_scoped_list_no_match_returns_empty(client):
    wid = _make_wiki(client, title="Launch Ops 3", wiki_id="launch-ops-3")
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "person", "title": "Alice"})
    r = client.get(f"/v1/wikis/{wid}/entities", params={"q": "zzz_no_such_thing"})
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_wiki_scoped_fact_and_relation(client):
    wid = _make_wiki(client, title="Snowflake Migration", wiki_id="snowflake-migration")
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "person", "title": "Alice Chen"})
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "project", "title": "Orion"})

    # Add a fact to a shared-wiki entity.
    r = client.post(f"/v1/wikis/{wid}/entities/person/alice-chen/facts", json={
        "text": "Works on Orion", "confidence": 0.9})
    assert r.status_code == 200, r.text
    facts = r.json()["facts"]
    assert any(f["text"] == "Works on Orion" for f in facts)

    # Link two entities.
    r = client.post(f"/v1/wikis/{wid}/entities/person/alice-chen/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
        "label": "works_on", "bidirectional": True})
    assert r.status_code == 200, r.text
    rels = r.json()["relations"]
    assert any(x["target"] == "project/orion" for x in rels)


def test_wiki_scoped_subgraph(client):
    wid = _make_wiki(client, title="Q3 Offsite", wiki_id="q3-team-offsite")
    for t in ("Alice Chen", "Bob Li", "Carol Wu"):
        client.put(f"/v1/wikis/{wid}/entities", json={"type": "person", "title": t})

    r = client.post(f"/v1/wikis/{wid}/subgraph", json={
        "entry_wiki_ids": ["person/alice-chen"], "max_depth": 1, "max_nodes": 10})
    assert r.status_code == 200, r.text
    names = [n["title"] for n in r.json()["nodes"]]
    assert "Alice Chen" in names


def test_wiki_scoped_subgraph_carries_component_and_degree_fields(client):
    """Regression: this route's ManifestEntryOut(**n) construction was
    silently dropping degree/hidden_neighbours (and now `component`) since
    pydantic v2 ignores unrecognised kwargs by default -- unlike the
    duplicate-route bug on the /v1/users/{user_id} side, this one has no
    other handler to fall back on, so the gap was fully live here."""
    wid = _make_wiki(client, title="Component Fields", wiki_id="component-fields")
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "person", "title": "Alice Chen"})
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "project", "title": "Orion"})
    client.post(f"/v1/wikis/{wid}/entities/person/alice-chen/relations",
               json={"target_wiki_id": "project/orion"})

    r = client.post(f"/v1/wikis/{wid}/subgraph",
                    json={"entry_wiki_ids": ["person/alice-chen"], "max_depth": 1})
    assert r.status_code == 200, r.text
    alice = next(n for n in r.json()["nodes"] if n["wiki_id"] == "person/alice-chen")
    assert isinstance(alice["component"], int)
    assert alice["degree"] == 1


def test_wiki_scoped_access_denies_unknown_wiki(client):
    r = client.put("/v1/wikis/does-not-exist/entities", json={
        "type": "person", "title": "Ghost"})
    assert r.status_code == 404, r.text


# ---------- grant enforcement (unit test of _grant with fabricated creds) ----------


def _grant_with(monkeypatch, wiki_id, caller, is_admin, role,
                hold=None, missing=False):
    """Drive _grant against a fake registry.

    hold: the role the caller actually holds on the wiki (None -> no grant).
    missing: the wiki does not exist at all.
    """
    calls = {}

    class FakeRegistry:
        def require(self, wid, user, role_, is_admin=False):
            calls["args"] = (wid, user, role_, is_admin)
            if missing:
                from app.wikis.registry import WikiNotFound
                raise WikiNotFound(f"No wiki {wid!r}.")
            granted = is_admin or (hold is not None and hold == role_)
            if not granted:
                from app.wikis.registry import WikiAccessDenied
                raise WikiAccessDenied(f"You do not have {role_} access to {wid!r}.")
            return None

    monkeypatch.setattr("app.api.routes_wiki_entities._registry", lambda: FakeRegistry())
    cred = {"user_id": caller, "is_admin": is_admin}
    return _grant(wiki_id, cred, role), calls


def test_grant_with_non_admin_writer(monkeypatch):
    # A user holding `write` on the wiki, not platform admin, passes through;
    # the registry is asked for write access as that user.
    _, calls = _grant_with(monkeypatch, "sales", "alice", False, ROLE_WRITE, hold=ROLE_WRITE)
    assert calls["args"] == ("sales", "alice", ROLE_WRITE, False)


def test_grant_admin_bypasses(monkeypatch):
    # Platform admin needs no per-wiki grant; is_admin=True is forwarded so
    # the registry bypasses its access list.
    _, calls = _grant_with(monkeypatch, "sales", "admin", True, ROLE_WRITE, hold=None)
    assert calls["args"] == ("sales", "admin", ROLE_WRITE, True)


def test_grant_missing_wiki_raises_404(monkeypatch):
    with pytest.raises(Exception) as ei:
        _grant_with(monkeypatch, "nope", "alice", False, ROLE_READ, hold=None,
                    missing=True)
    from fastapi import HTTPException
    assert isinstance(ei.value, HTTPException)
    assert ei.value.status_code == 404


def test_grant_denied_raises_403(monkeypatch):
    # bob has no grant at all, so write is refused.
    with pytest.raises(Exception) as ei:
        _grant_with(monkeypatch, "sales", "bob", False, ROLE_WRITE, hold=None)
    from fastapi import HTTPException
    assert isinstance(ei.value, HTTPException)
    assert ei.value.status_code == 403


def test_grant_read_denied_when_only_write(monkeypatch):
    # carol holds `write` but that does not grant `admin`; enforce rank.
    with pytest.raises(Exception) as ei:
        _grant_with(monkeypatch, "sales", "carol", False, ROLE_ADMIN,
                    hold=ROLE_WRITE)
    from fastapi import HTTPException
    assert isinstance(ei.value, HTTPException)
    assert ei.value.status_code == 403


# ---------- collaboration: activity feed + membership ----------

def test_activity_feed_records_actor(client):
    wid = _make_wiki(client, title="Onboarding Flow", wiki_id="onboarding-flow-design")
    client.put(f"/v1/wikis/{wid}/entities", json={"type": "person", "title": "Alice Chen"})
    client.post(f"/v1/wikis/{wid}/entities/person/alice-chen/facts", json={
        "text": "Owns the onboarding flow"})

    r = client.get(f"/v1/wikis/{wid}/activity?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wiki_id"] == wid
    assert len(body["records"]) >= 1
    record = body["records"][0]
    assert "actor" in record            # who did it
    assert record["actor"] == "admin"  # AUTH_MODE=off caller is platform admin
    assert record["wiki_id"]            # what entity was touched
    assert record["created_at"]
    assert record["op"] in ("create", "update_fact")


def test_members_lists_grants_and_requires_admin(client):
    wid = _make_wiki(client, title="Q3 Offsite", wiki_id="q3-team-offsite")
    # Grant a teammate via the wiki access endpoint (platform admin can).
    r = client.post(f"/v1/wikis/{wid}/access", json={"user_id": "alice", "role": "write"})
    assert r.status_code == 200, r.text
    # Grant another as read-only.
    client.post(f"/v1/wikis/{wid}/access", json={"user_id": "bob", "role": "read"})

    # Platform admin (AUTH_MODE=off) can list members.
    r = client.get(f"/v1/wikis/{wid}/members")
    assert r.status_code == 200, r.text
    body = r.json()
    roles = {m["user_id"]: m["role"] for m in body["members"]}
    assert roles["alice"] == "write"
    assert roles["bob"] == "read"
    assert roles["admin"] == "admin"  # creator


def test_entities_persist_across_collaborators(client):
    """Two users write into the same wiki and both see everything — the core
    collaborative invariant. (AUTH_MODE=off makes both callers 'admin', but
    the point is the shared-scope reads/writes land in one wiki.)"""
    wid = _make_wiki(client, title="Snowflake Migration", wiki_id="snowflake-migration")
    client.put(f"/v1/wikis/{wid}/entities", json={
        "type": "person", "title": "Alice Chen", "summary_append": "from alice"})
    client.put(f"/v1/wikis/{wid}/entities", json={
        "type": "person", "title": "priya", "summary_append": "from colleague"})
    client.post(f"/v1/wikis/{wid}/entities/person/priya/facts", json={"text": "leads migration"})

    # A fresh read sees both contributors' nodes in one wiki (the whole-scope
    # entity list, unlike a subgraph which only traverses from a seed).
    r = client.get(f"/v1/wikis/{wid}/entities")
    assert r.status_code == 200, r.text
    titles = [e["title"] for e in r.json()]
    assert "Alice Chen" in titles
    assert "priya" in titles
