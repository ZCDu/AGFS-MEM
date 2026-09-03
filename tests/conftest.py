"""
Shared test setup.

AUTH_MODE defaults to "token" and the app refuses to start without tokens, so
importing anything that builds the app fails at collection time unless auth is
configured. That fail-closed behaviour is deliberate and is itself tested in
tests/test_auth.py — the rest of the suite is about storage and graph
behaviour, so it runs with auth off.

Set at session scope via an autouse fixture rather than inside individual
fixtures, because the failure happens during MODULE IMPORT (app/main.py calls
create_app at import), which is earlier than any per-test fixture runs.
"""

from __future__ import annotations

import os

import pytest

# Must happen before any test module imports app.main.
os.environ.setdefault("AUTH_MODE", "off")


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    """Keep auth off for every test that does not explicitly opt in.

    tests/test_auth.py overrides these with monkeypatch and resets the cached
    Settings, so it exercises token mode without leaking into other tests.
    """
    monkeypatch.setenv("AUTH_MODE", "off")
    monkeypatch.delenv("AUTH_TOKENS", raising=False)
    monkeypatch.delenv("AUTH_ADMIN_TOKEN", raising=False)
    import app.config as cfg
    cfg._settings = None
    yield
    cfg._settings = None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over a throwaway disk-backed store.

    Lives here rather than in one test module so any module can request it.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", str(tmp_path / "bucket"))
    import app.config as config_module
    import app.deps as deps_module
    config_module._settings = None
    deps_module.get_storage_backend.cache_clear()

    from fastapi.testclient import TestClient
    from app.main import create_app

    # Use TestClient as a context manager so FastAPI's lifespan runs. The
    # lifespan is what calls backend.close() (which drains the write-behind
    # buffers and stops the mirage event loop). Without it the backend and
    # its event loop leak past the test, background flush timers keep firing,
    # and at interpreter shutdown they try to write through a dead aiofiles
    # executor -> a flood of "cannot schedule new futures after shutdown"
    # tracebacks that do not fail any test but bury the summary.
    with TestClient(create_app()) as test_client:
        yield test_client
    deps_module.get_storage_backend.cache_clear()
