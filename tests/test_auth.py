"""
Authentication tests.

The property that matters most is NEGATIVE: a token bound to one user must not
reach another user's data. Before auth existed, `user_id` was just a path
parameter, so editing the URL was enough to read anyone's memory.

These deliberately re-import app.main with different environments, because
fail-closed startup validation runs in create_app() — testing it means testing
construction, not just requests.
"""

from __future__ import annotations

import importlib
import tempfile

import pytest
from fastapi.testclient import TestClient

import app.config as cfg
from app.auth import parse_token_map, suggest_token


# Backends created by build() below are never closed in the test body, and
# leaving them alive means their write-behind flush timers fire at interpreter
# shutdown through a dead aiofiles executor — a flood of "cannot schedule new
# futures after shutdown" tracebacks that bury the summary. This autouse
# fixture closes every leaked backend as the test that created it completes.
_BACKENDS = []


@pytest.fixture(autouse=True)
def _close_leaked_backends():
    yield
    import app.deps as deps
    for b in _BACKENDS:
        try:
            close = getattr(b, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
    _BACKENDS.clear()
    deps.get_storage_backend.cache_clear()


def build(monkeypatch, **env) -> TestClient:
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", tempfile.mkdtemp())
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    cfg._settings = None
    import app.deps as deps
    deps.get_storage_backend.cache_clear()
    main = importlib.reload(importlib.import_module("app.main"))
    client = TestClient(main.create_app())
    # Track the singleton backend so _close_leaked_backends() drains its
    # write-behind buffers and stops its event loop before process exit.
    _BACKENDS.append(deps.get_storage_backend())
    return client


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- fail closed ----------

def test_token_mode_without_tokens_refuses_to_start(monkeypatch):
    """An unauthenticated API must never ship by accident. The failure has to
    happen loudly at startup, not silently at runtime."""
    with pytest.raises(RuntimeError) as exc:
        # AUTH_SECRET, AUTH_TOKENS and AUTH_ADMIN_TOKEN must all be forced to
        # a *present-but-empty* value, not deleted: the reload re-runs
        # load_dotenv(), which would re-inject the real values from .env (the
        # normal live setup) and exit the "nothing configured" branch. An
        # empty string survives dotenv (it does not override existing vars)
        # and config's `get(...) or None` reads it as absent, so this
        # reliably exercises the totally-unconfigured state.
        build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="",
              AUTH_ADMIN_TOKEN="", AUTH_SECRET="")
    msg = str(exc.value)
    assert "no tokens are configured" in msg
    assert "AUTH_TOKENS=" in msg, "the error must show how to fix it"
    assert "AUTH_MODE=off" in msg, "and mention the dev escape hatch"


def test_unknown_auth_mode_refuses_to_start(monkeypatch):
    with pytest.raises(RuntimeError, match="not recognised"):
        build(monkeypatch, AUTH_MODE="yes-please", AUTH_TOKENS="t:u")


def test_malformed_token_map_refuses_to_start(monkeypatch):
    with pytest.raises(RuntimeError, match="malformed"):
        build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="no-colon-here")


# ---------- the negative property ----------

def test_a_token_cannot_reach_another_users_data(monkeypatch):
    c = build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="tok-alice:alice,tok-bob:bob")

    assert c.put("/v1/users/alice/wiki", headers=hdr("tok-alice"),
                 json={"type": "person", "title": "Secret"}).status_code == 200

    # Bob holds a valid token. That must not be enough.
    assert c.get("/v1/users/alice/wiki", headers=hdr("tok-bob")).status_code == 403
    assert c.put("/v1/users/alice/wiki", headers=hdr("tok-bob"),
                 json={"type": "person", "title": "Injected"}).status_code == 403
    assert c.delete("/v1/users/alice/wiki/person/secret",
                    headers=hdr("tok-bob")).status_code == 403

    # And Alice's data is untouched.
    rows = c.get("/v1/users/alice/wiki", headers=hdr("tok-alice")).json()
    assert [r["title"] for r in rows] == ["Secret"]


def test_missing_and_bad_tokens_are_rejected(monkeypatch):
    c = build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="tok-alice:alice")

    no_auth = c.get("/v1/users/alice/wiki")
    assert no_auth.status_code == 401
    assert no_auth.headers.get("www-authenticate") == "Bearer", \
        "401 must advertise the scheme per RFC 7235"

    assert c.get("/v1/users/alice/wiki", headers=hdr("wrong")).status_code == 401
    assert c.get("/v1/users/alice/wiki",
                 headers={"Authorization": "tok-alice"}).status_code == 401, \
        "a bare token without the Bearer prefix is not valid"


def test_every_data_router_is_protected(monkeypatch):
    """Auth is attached at router level so a new endpoint is protected by
    default. This asserts all three routers actually carry it."""
    c = build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="tok:alice")
    for method, path, body in [
        ("get", "/v1/users/alice/wiki", None),
        ("get", "/v1/users/alice/wiki/_stats", None),
        ("post", "/v1/users/alice/wiki/traverse", {"entry_wiki_ids": ["person/x"]}),
        ("post", "/v1/users/alice/wiki/_reconcile", None),
        ("post", "/v1/users/alice/assess", {"text": "some text to score here"}),
        ("get", "/v1/users/alice/raw-facts?on=2026-07-30", None),
        ("post", "/v1/users/alice/raw-facts", {"facts": [{"a": 1}]}),
    ]:
        r = getattr(c, method)(path, json=body) if body else getattr(c, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} was reachable without a token"


# ---------- admin and rotation ----------

def test_admin_token_reaches_any_user(monkeypatch):
    """Cross-tenant maintenance needs one credential; per-user tokens cannot
    iterate over users they are not scoped to."""
    c = build(monkeypatch, AUTH_MODE="token",
              AUTH_TOKENS="tok-alice:alice", AUTH_ADMIN_TOKEN="tok-admin")
    for user in ("alice", "bob", "anyone-at-all"):
        assert c.get(f"/v1/users/{user}/wiki", headers=hdr("tok-admin")).status_code == 200


def test_two_tokens_may_map_to_one_user_for_rotation(monkeypatch):
    """Rotation without downtime: add the new token, drain, remove the old."""
    c = build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="old:alice,new:alice")
    assert c.get("/v1/users/alice/wiki", headers=hdr("old")).status_code == 200
    assert c.get("/v1/users/alice/wiki", headers=hdr("new")).status_code == 200


# ---------- open by design ----------

def test_health_docs_and_gui_stay_open(monkeypatch):
    """Monitoring needs healthz; /docs and /gui are static and carry no user
    data. Every API call the GUI makes is still checked."""
    c = build(monkeypatch, AUTH_MODE="token", AUTH_TOKENS="tok:alice")
    assert c.get("/healthz").status_code == 200
    assert c.get("/gui").status_code == 200
    assert c.get("/docs").status_code == 200


def test_auth_off_allows_everything_but_is_opt_in(monkeypatch):
    c = build(monkeypatch, AUTH_MODE="off", AUTH_TOKENS=None)
    assert c.get("/v1/users/anyone/wiki").status_code == 200


# ---------- helpers ----------

def test_token_map_parsing():
    assert parse_token_map("a:1, b:2 ,") == {"a": "1", "b": "2"}
    assert parse_token_map("") == {}
    assert parse_token_map("t:user:with:colons") == {"t": "user:with:colons"}
    for bad in ("nocolon", ":user", "token:"):
        with pytest.raises(ValueError):
            parse_token_map(bad)


def test_suggested_tokens_are_unguessable():
    a, b = suggest_token(), suggest_token()
    assert a != b
    assert len(a) >= 32
