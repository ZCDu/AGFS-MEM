"""Self-service registration tests.

Registration is the user-creation system: a caller signs up with a username
and password and is logged in immediately. It must fail closed in off/None
auth, reject duplicates and weak passwords, all without leaking whether an
account exists, and must NOT create an admin or grant any wiki access.
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
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    import app.api.routes_auth as ra
    ra._throttle = ra.LoginThrottle()

    store = UserStore(deps.get_storage_backend())
    with TestClient(main.create_app()) as client:
        yield client, store
    deps.get_storage_backend.cache_clear()


def test_register_creates_user_and_logs_in(app_and_store):
    client, store = app_and_store
    r = client.post("/v1/auth/register",
                    json={"username": "Xavier", "password": "correct-horse-9"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "xavier"          # normalised to lowercase
    assert body["user_id"] == "xavier"
    assert body["is_admin"] is False
    assert body["token"]                         # logged in immediately
    assert body["token_type"] == "bearer"
    assert body["expires_at"] > 0
    # The account really exists and authenticates with the password.
    user = store.authenticate("xavier", "correct-horse-9")
    assert user is not None and user.user_id == "xavier"
    # The session token works to reach a user-scoped route.
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200


def test_register_rejects_duplicate_username(app_and_store):
    client, _ = app_and_store
    j = {"username": "dupe", "password": "a-strong-password-1"}
    assert client.post("/v1/auth/register", json=j).status_code == 201
    r = client.post("/v1/auth/register", json=j)
    assert r.status_code == 409, r.text          # duplicate -> conflict


def test_register_rejects_weak_password(app_and_store):
    client, _ = app_and_store
    r = client.post("/v1/auth/register",
                    json={"username": "weakling", "password": "short"})
    assert r.status_code == 409, r.text


def test_register_is_scoped_as_normal_user_no_wikis(app_and_store):
    client, _ = app_and_store
    r = client.post("/v1/auth/register",
                    json={"username": "yolanda", "password": "another-pass-77"})
    token = r.json()["token"]
    # A brand-new user can list wikis but has none, and nothing is granted.
    lst = client.get("/v1/wikis", headers={"Authorization": f"Bearer {token}"})
    assert lst.status_code == 200
    assert lst.json() == []


def test_register_fails_closed_when_auth_off(monkeypatch):
    # With AUTH_MODE=off there is nothing to register for.
    root = tempfile.mkdtemp()
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", root)
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.setenv("AUTH_SECRET", SECRET)
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    with TestClient(main.create_app()) as client:
        r = client.post("/v1/auth/register",
                        json={"username": "q", "password": "a-strong-pass-99"})
        assert r.status_code == 400, r.text
    deps.get_storage_backend.cache_clear()
