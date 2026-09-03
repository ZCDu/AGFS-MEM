"""
Username/password login tests.

The properties worth pinning are mostly negative or about leakage: that a
tampered token is rejected, that failures are indistinguishable from each
other, that a session is scoped to one user, and that brute force is throttled.
"""

from __future__ import annotations

import importlib
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

import app.config as cfg
from app.users import (MIN_PASSWORD_LENGTH, SessionError, UserStore, hash_password,
                       issue_session_token, verify_password, verify_session_token)

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
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    # Reset the module-level login throttle so tests don't poison each other.
    import app.api.routes_auth as ra
    ra._throttle = ra.LoginThrottle()

    store = UserStore(deps.get_storage_backend())
    store.create("alice", "hunter2hunter2")
    store.create("ops", "adminpassword1", is_admin=True)
    # Context-managed TestClient so the lifespan runs backend.close() (drains
    # write-behind buffers, stops the mirage event loop); clear the cache on
    # teardown so the singleton backend does not leak past this test and its
    # flush timers do not fire at interpreter shutdown.
    with TestClient(main.create_app()) as client:
        yield client, store
    deps.get_storage_backend.cache_clear()


def hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def login(c, username, password):
    return c.post("/v1/auth/login", json={"username": username, "password": password})


# ---------- hashing ----------

def test_passwords_are_salted_so_identical_inputs_differ():
    a, b = hash_password("same password here"), hash_password("same password here")
    assert a["salt"] != b["salt"]
    assert a["hash"] != b["hash"], "no salt means one rainbow table covers every account"
    assert verify_password("same password here", a)
    assert verify_password("same password here", b)


def test_hash_record_carries_its_own_parameters():
    """So scrypt cost can be raised later without invalidating existing accounts."""
    rec = hash_password("some password")
    assert {"algo", "n", "r", "p", "dklen", "salt", "hash"} <= set(rec)
    assert rec["algo"] == "scrypt"


def test_verify_rejects_wrong_password_and_corrupt_records():
    rec = hash_password("correct password")
    assert not verify_password("wrong password", rec)
    assert not verify_password("correct password", {})
    assert not verify_password("correct password", {**rec, "algo": "md5"})
    assert not verify_password("correct password", {**rec, "salt": "!!!"})


def test_short_passwords_are_refused(app_and_store):
    _, store = app_and_store
    with pytest.raises(ValueError, match=str(MIN_PASSWORD_LENGTH)):
        store.create("shorty", "abc")


# ---------- session tokens ----------

def test_session_token_signature_is_enforced():
    tok, _ = issue_session_token(SECRET, "alice", 1)
    assert verify_session_token(SECRET, tok)["u"] == "alice"
    with pytest.raises(SessionError, match="signature"):
        verify_session_token("a-different-secret", tok)
    with pytest.raises(SessionError, match="signature"):
        verify_session_token(SECRET, tok[:-4] + "AAAA")


def test_expired_session_is_rejected():
    tok, _ = issue_session_token(SECRET, "alice", -1)
    with pytest.raises(SessionError, match="expired"):
        verify_session_token(SECRET, tok)


def test_rotating_the_secret_invalidates_every_session():
    """The only bulk revocation mechanism, since sessions are stateless."""
    tok, _ = issue_session_token(SECRET, "alice", 12)
    with pytest.raises(SessionError):
        verify_session_token(SECRET + "-rotated", tok)


# ---------- login ----------

def test_login_returns_a_working_token(app_and_store):
    c, _ = app_and_store
    r = login(c, "alice", "hunter2hunter2")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "alice" and body["is_admin"] is False
    assert body["expires_at"] > time.time()

    assert c.get("/v1/users/alice/wiki", headers=hdr(body["token"])).status_code == 200


def test_login_failures_are_indistinguishable(app_and_store):
    """Unknown user, wrong password and disabled account must look identical —
    whether an account exists is not for an unauthenticated caller to learn."""
    c, store = app_and_store
    store.create("frozen", "frozenpassword1")
    store.set_disabled("frozen", True)

    bodies = set()
    for u, p in [("alice", "wrong-password"), ("ghost", "any-password"),
                 ("frozen", "frozenpassword1")]:
        r = login(c, u, p)
        assert r.status_code == 401
        bodies.add(r.json()["detail"])
    assert len(bodies) == 1, f"login leaks which failure occurred: {bodies}"


def test_username_is_case_insensitive(app_and_store):
    c, _ = app_and_store
    assert login(c, "ALICE", "hunter2hunter2").status_code == 200


def test_session_is_scoped_to_one_user(app_and_store):
    c, _ = app_and_store
    tok = login(c, "alice", "hunter2hunter2").json()["token"]
    assert c.get("/v1/users/alice/wiki", headers=hdr(tok)).status_code == 200
    assert c.get("/v1/users/ops/wiki", headers=hdr(tok)).status_code == 403


def test_admin_account_reaches_any_user(app_and_store):
    c, _ = app_and_store
    tok = login(c, "ops", "adminpassword1").json()["token"]
    for user in ("alice", "bob", "nobody"):
        assert c.get(f"/v1/users/{user}/wiki", headers=hdr(tok)).status_code == 200


def test_brute_force_is_throttled(app_and_store):
    """scrypt protects the stored hash but does nothing to slow an attacker
    hammering the endpoint — and in fact makes it a cheap DoS."""
    c, _ = app_and_store
    codes = [login(c, "alice", "nope").status_code for _ in range(12)]
    assert 429 in codes, "login is brute-forceable at network speed"
    assert codes.index(429) <= 9

    r = c.post("/v1/auth/login", json={"username": "alice", "password": "nope"})
    assert r.headers.get("retry-after"), "429 should say when to retry"


def test_whoami_reports_the_credential(app_and_store):
    """The quickest way to debug a 403: which user_id is this token actually for?"""
    c, _ = app_and_store
    tok = login(c, "alice", "hunter2hunter2").json()["token"]
    me = c.get("/v1/auth/me", headers=hdr(tok)).json()
    assert me == {"auth": "session", "user_id": "alice", "is_admin": False}
    assert c.get("/v1/auth/me").status_code == 401


# ---------- change password ----------

def test_change_password(app_and_store):
    c, _ = app_and_store
    tok = login(c, "alice", "hunter2hunter2").json()["token"]

    assert c.post("/v1/auth/password", headers=hdr(tok),
                  json={"current_password": "wrong",
                        "new_password": "brandnewpassword"}).status_code == 401
    assert c.post("/v1/auth/password", headers=hdr(tok),
                  json={"current_password": "hunter2hunter2",
                        "new_password": "short"}).status_code == 422
    assert c.post("/v1/auth/password", headers=hdr(tok),
                  json={"current_password": "hunter2hunter2",
                        "new_password": "brandnewpassword"}).status_code == 204

    assert login(c, "alice", "hunter2hunter2").status_code == 401
    assert login(c, "alice", "brandnewpassword").status_code == 200


def test_static_tokens_cannot_change_passwords(monkeypatch):
    """A static token is not attached to an account, so there is no password
    for it to change."""
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", tempfile.mkdtemp())
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKENS", "static-tok:alice")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    with TestClient(main.create_app()) as c:
        assert c.get("/v1/users/alice/wiki", headers=hdr("static-tok")).status_code == 200
        assert c.post("/v1/auth/password", headers=hdr("static-tok"),
                      json={"current_password": "x", "new_password": "yyyyyyyyyyyy"}
                      ).status_code == 401
    deps.get_storage_backend.cache_clear()


def test_secret_alone_satisfies_startup_validation(monkeypatch):
    """AUTH_SECRET is a valid way to configure auth — password accounts instead
    of static tokens."""
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", tempfile.mkdtemp())
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    monkeypatch.delenv("AUTH_TOKENS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_TOKEN", raising=False)
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    assert main.create_app() is not None
