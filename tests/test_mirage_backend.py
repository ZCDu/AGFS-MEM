"""
Exercises EntityGraphStore (OKF schema) through MirageBackend, the same way
test_api.py exercises it through LocalFSBackend. See that file's docstring
for why DiskResource stands in for S3Resource here.
"""

from __future__ import annotations

import shutil

import pytest

mirage = pytest.importorskip("mirage", reason="mirage-ai not installed")

from app.graph.store import EntityGraphStore
from app.rawlog.log import RawFactLog
from app.storage.mirage_backend import MirageBackend, _EventLoopThread


@pytest.fixture()
def mirage_backend(tmp_path):
    root = str(tmp_path / "mirage_disk_root")
    loop_thread = _EventLoopThread()

    async def _build():
        from mirage import DiskResource, MountMode, Workspace
        return Workspace({"/s3": DiskResource(root=root)}, mode=MountMode.WRITE)

    ws = loop_thread.run(_build())
    backend = MirageBackend(ws, object_mount="/s3", owns_workspace=True, loop_thread=loop_thread)
    yield backend
    backend.close()
    shutil.rmtree(root, ignore_errors=True)


def test_entity_roundtrip_via_mirage(mirage_backend):
    store = EntityGraphStore(mirage_backend)
    entity = store.upsert_entity("u1", "person", "Alice Chen", summary_append="Engineer.")
    assert entity.title == "Alice Chen"
    assert entity.wiki_id == "person/alice-chen"

    fetched = store.get_entity("u1", "person/alice-chen")
    assert fetched is not None
    assert fetched.summary == "Engineer."


def test_link_and_traverse_via_mirage(mirage_backend):
    store = EntityGraphStore(mirage_backend)
    store.upsert_entity("u1", "person", "Alice")
    store.upsert_entity("u1", "project", "Orion")
    store.upsert_entity("u1", "artifact", "AWS")
    store.link_entities("u1", "person/alice", "project/orion", category="related_to")
    store.link_entities("u1", "project/orion", "artifact/aws", category="related_to")

    reached = store.traverse("u1", ["person/alice"], max_depth=2)
    assert reached == {"person/alice", "project/orion", "artifact/aws"}


def test_concurrent_write_conflict_detected_via_mirage(mirage_backend):
    store = EntityGraphStore(mirage_backend)
    store.upsert_entity("u1", "person", "Alice")
    store.upsert_entity("u1", "project", "Orion")
    store.upsert_entity("u1", "artifact", "AWS")

    # Simulate two writers reading the same version, then both writing —
    # the retry loop in _mutate() should still land both updates because
    # it re-reads on conflict (see MAX_WRITE_RETRIES in app/graph/store.py).
    store.link_entities("u1", "person/alice", "project/orion", category="related_to", reason="writer A")
    store.link_entities("u1", "person/alice", "artifact/aws", category="related_to", reason="writer B")

    entity = store.get_entity("u1", "person/alice", touch=False)
    targets = {r.target for r in entity.relations}
    assert targets == {"project/orion", "artifact/aws"}, (
        "both relations should have survived — the retry loop must re-read "
        "on conflict rather than silently dropping the losing writer's update"
    )


def test_manifest_and_ops_log_via_mirage(mirage_backend):
    store = EntityGraphStore(mirage_backend)
    store.upsert_entity("u1", "person", "Alice", summary_append="Engineer.")

    entries = store.manifest.list_entries("u1")
    assert len(entries) == 1
    assert entries[0].wiki_id == "person/alice"
    assert entries[0].compact == "Engineer."

    from datetime import date
    ops = store.ops_log.read_day("u1", date.today())
    assert len(ops) >= 1
    assert ops[0]["op"] == "create"
    assert ops[0]["wiki_id"] == "person/alice"


def test_raw_log_via_mirage(mirage_backend):
    log = RawFactLog(mirage_backend)
    log.append_batch("u1", [{"fact": "hello"}, {"fact": "world"}])

    from datetime import date
    records = log.read_day("u1", date.today())
    assert len(records) == 2
    assert {r["fact"] for r in records} == {"hello", "world"}


def test_listings_are_not_stale_across_processes(tmp_path):
    """Mirage's index cache defaults to a 600s TTL, and a Workspace does not
    see another process's writes in a listing until it expires — under both
    ConsistencyPolicy.LAZY and ALWAYS. Only ttl=0 is fresh.

    That is not a performance detail here. WikiManifest reads its delta chain
    by listing, WikiOpsLog.read_day lists segments, and rebuild() REPLACES the
    index with whatever the listing returns — so a stale listing during
    rebuild deletes entries for entities that exist, in the recovery path.
    """
    import os
    from app.storage.mirage_backend import MirageBackend

    root = str(tmp_path / "bucket")
    backend = MirageBackend.from_disk(root=root)
    try:
        backend.put_bytes("wikis/u/_manifest/d/001.json", b'{"upsert":{}}')
        assert len(backend.list_keys("wikis/u/_manifest/d/")) == 1

        # Simulate a second worker writing straight to the same bucket.
        os.makedirs(os.path.join(root, "wikis", "u", "_manifest", "d"), exist_ok=True)
        with open(os.path.join(root, "wikis", "u", "_manifest", "d", "002.json"), "wb") as f:
            f.write(b'{"upsert":{}}')

        keys = backend.list_keys("wikis/u/_manifest/d/")
        assert len(keys) == 2, f"stale listing: index cache TTL must be 0, got {keys}"
    finally:
        backend.close()


def test_index_ttl_is_configurable_and_defaults_to_zero(tmp_path):
    from app.config import Settings
    from app.storage.mirage_backend import DEFAULT_INDEX_TTL

    assert DEFAULT_INDEX_TTL == 0.0
    assert Settings.from_env().mirage_index_ttl_seconds == 0.0


def test_connection_reuse_creates_one_client_for_many_ops():
    """mirage/core/s3/_client.py builds a new aioboto3 Session and client for
    EVERY operation, so each read/write/list pays a full DNS+TCP+TLS
    handshake. Against a remote endpoint that is a flat per-op floor —
    measured ~600ms for GET, PUT, DELETE and LIST alike, independent of
    payload. The pooling patch must collapse that to one client."""
    import asyncio
    import app.storage.mirage_backend as mb

    created = {"n": 0}

    class FakeClient:
        pass

    class FakeCtx:
        async def __aenter__(self):
            created["n"] += 1
            return FakeClient()

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def client(self, **k):
            return FakeCtx()

    async def run(factory, n):
        for _ in range(n):
            async with factory().client(service_name="s3", endpoint_url="https://x") as _c:
                pass

    created["n"] = 0
    asyncio.run(run(lambda: FakeSession(), 10))
    assert created["n"] == 10, "sanity: stock behaviour is one client per op"

    created["n"] = 0
    mb._pooled.clear()
    try:
        asyncio.run(run(lambda: mb._PooledSession(FakeSession()), 10))
        assert created["n"] == 1, f"expected connection reuse, got {created['n']} clients"
    finally:
        mb._pooled.clear()


def test_connection_reuse_patch_reaches_core_op_modules():
    """The core op modules import async_session by name at import time, so
    rebinding it only on _client would leave read/write/stat unpatched."""
    import app.storage.mirage_backend as mb
    mb._install_connection_reuse()

    from mirage.core.s3 import _client, read, write
    assert _client.async_session.__name__ == "pooled_async_session"
    assert read.async_session is _client.async_session
    assert write.async_session is _client.async_session
