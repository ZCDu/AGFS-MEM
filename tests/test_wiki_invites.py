"""Passcode invite / join tests.

Meeting/project wikis use invites as the conventional way to grant access: the
owner issues a passcode for a role, a user redeems it to gain that role. Tests
cover create/redeem, expiry, use-limits, revocation, and that only valid
passcodes grant access.
"""

from __future__ import annotations

import importlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app.config as cfg
from app.users import UserStore

SECRET = "unit-test-signing-secret"


@pytest.fixture()
def app_and_store(monkeypatch):
    root = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", root)
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    monkeypatch.delenv("AUTH_TOKENS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("WIKI_CREATE_REQUIRES_ADMIN", "false")
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    import app.api.routes_auth as ra
    ra._throttle = ra.LoginThrottle()

    store = UserStore(deps.get_storage_backend())
    # alice owns the wiki.
    alice = store.create("alice", "hunter2hunter2")
    with TestClient(main.create_app()) as client:
        r = client.post("/v1/auth/login",
                        json={"username": "alice", "password": "hunter2hunter2"})
        alice_tok = r.json()["token"]
        yield client, alice_tok, store, alice
    deps.get_storage_backend.cache_clear()


def hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def _create_meeting(client, tok, title="Q3 Planning"):
    r = client.post("/v1/wikis",
                    json={"title": title},
                    headers=hdr(tok))
    assert r.status_code == 201, r.text
    return r.json()["wiki_id"]


def _register(client, name, pw):
    return client.post("/v1/auth/register",
                       json={"username": name, "password": pw}).json()


def test_invite_and_join_grant_write(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)

    inv = client.post(f"/v1/wikis/{wid}/invite",
                      json={"role": "write"}, headers=hdr(alice_tok))
    assert inv.status_code == 201, inv.text
    code = inv.json()["passcode"]

    # A brand-new user joins with the passcode.
    bob = _register(client, "bob", "bob-password-1")
    rst = client.post(f"/v1/wikis/{wid}/join",
                      json={"passcode": code}, headers=hdr(bob["token"]))
    assert rst.status_code == 200, rst.text
    assert rst.json()["access"]["bob"] == "write"

    # Bob can now list the wiki (it appears in his reachable set).
    lst = client.get("/v1/wikis", headers=hdr(bob["token"]))
    ids = [w["wiki_id"] for w in lst.json()]
    assert wid in ids


def test_join_bad_passcode_is_rejected(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)
    carol = _register(client, "carol", "carol-password-1")
    r = client.post(f"/v1/wikis/{wid}/join",
                    json={"passcode": "wrongcode"}, headers=hdr(carol["token"]))
    assert r.status_code == 422, r.text


def test_invite_uses_left_is_consumed(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)
    inv = client.post(f"/v1/wikis/{wid}/invite",
                      json={"role": "read", "uses_left": 1}, headers=hdr(alice_tok))
    code = inv.json()["passcode"]

    dave = _register(client, "dave", "dave-password-1")
    r1 = client.post(f"/v1/wikis/{wid}/join",
                     json={"passcode": code}, headers=hdr(dave["token"]))
    assert r1.status_code == 200

    eve = _register(client, "eve", "eve-password-1")
    r2 = client.post(f"/v1/wikis/{wid}/join",
                     json={"passcode": code}, headers=hdr(eve["token"]))
    assert r2.status_code == 422, r2.text   # exhausted


def test_invite_expiry(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)
    inv = client.post(f"/v1/wikis/{wid}/invite",
                      json={"role": "write", "expires_in": 2}, headers=hdr(alice_tok))
    code = inv.json()["passcode"]

    import time as _t
    _t.sleep(3.0)
    frank = _register(client, "frank", "frank-password-1")
    r = client.post(f"/v1/wikis/{wid}/join",
                    json={"passcode": code}, headers=hdr(frank["token"]))
    assert r.status_code == 422, r.text


def test_revoke_invite_blocks_redeem(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)
    inv = client.post(f"/v1/wikis/{wid}/invite",
                      json={"role": "write"}, headers=hdr(alice_tok))
    code = inv.json()["passcode"]

    rev = client.delete(f"/v1/wikis/{wid}/invite/{code}", headers=hdr(alice_tok))
    assert rev.status_code == 200

    gus = _register(client, "gus", "gus-password-1")
    r = client.post(f"/v1/wikis/{wid}/join",
                    json={"passcode": code}, headers=hdr(gus["token"]))
    assert r.status_code == 422, r.text


def test_non_admin_cannot_create_invite(app_and_store):
    client, alice_tok, store, _ = app_and_store
    wid = _create_meeting(client, alice_tok)
    # Give bob write (not admin) via a direct grant by alice.
    client.post(f"/v1/wikis/{wid}/access",
                json={"user_id": "bob", "role": "write"}, headers=hdr(alice_tok))
    bob = _register(client, "bob2", "bob2-password-1")
    r = client.post(f"/v1/wikis/{wid}/invite",
                    json={"role": "write"}, headers=hdr(bob["token"]))
    assert r.status_code == 403, r.text      # write cannot grant

