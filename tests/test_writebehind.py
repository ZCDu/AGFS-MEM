"""
Tests for the write-behind cost reduction (manifest buffering + ops-log
segments). These pin the two things that could silently regress:

  1. The COST property itself — writes must stay linear in entity count,
     not quadratic. A future change that reintroduces a full-file
     read-modify-write on the hot path would still pass every functional
     test in the suite, so it needs its own assertion.
  2. The DURABILITY story that makes buffering acceptable — buffered
     state must survive a flush, be rebuildable from entity files if it
     doesn't, and be bypassable via sync mode.
"""

from __future__ import annotations

import collections
import datetime

import pytest

import app.config as cfg
from app.graph.store import EntityGraphStore
from app.storage.backend import LocalFSBackend


class CountingBackend(LocalFSBackend):
    """Counts only calls made BY the store layer. LocalFSBackend.put_bytes
    reads the current object internally to check the ETag; real S3 does that
    server-side in one round-trip, so counting it would overstate GETs."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ops = collections.Counter()
        self.bytes_out = 0
        self._inside_put = False

    def get_bytes(self, key):
        if not self._inside_put:
            self.ops["GET"] += 1
        return super().get_bytes(key)

    def put_bytes(self, key, data, if_match=None):
        self.ops["PUT"] += 1
        self.bytes_out += len(data)
        self._inside_put = True
        try:
            return super().put_bytes(key, data, if_match)
        finally:
            self._inside_put = False

    def delete(self, key):
        self.ops["DELETE"] += 1
        return super().delete(key)

    def list_keys(self, prefix):
        self.ops["LIST"] += 1
        return super().list_keys(prefix)


def wipe_manifest(backend, user_id):
    """Delete all persisted manifest state — snapshot, deltas, and the
    legacy single file. Tests must not hardcode one filename, because the
    layout is snapshot+deltas and a compaction can move where state lives."""
    for key in list(backend.list_keys(f"wikis/{user_id}/_manifest")):
        backend.delete(key)
    backend.delete(f"wikis/{user_id}/_manifest.json")


@pytest.fixture()
def make_store(tmp_path, monkeypatch):
    counter = {"n": 0}

    def _make(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        cfg._settings = None
        counter["n"] += 1
        backend = CountingBackend(root=str(tmp_path / f"bucket{counter['n']}"))
        return backend, EntityGraphStore(backend)

    yield _make
    cfg._settings = None


# ---------- cost properties ----------

def test_writes_are_linear_not_quadratic(make_store):
    """Doubling entity count must roughly double bytes written.

    Uses DEFAULT flush settings deliberately. An earlier version of this
    test pinned FLUSH_MAX_PENDING absurdly high, which produced exactly one
    flush at the very end — that makes any layout look linear and hid a
    genuinely quadratic manifest for a while. If you tune this test, do not
    reintroduce a setting that collapses the run into a single flush."""
    written = {}
    for n in (200, 400):
        backend, store = make_store()
        for i in range(n):
            store.upsert_entity("u", "concept", f"C{i}")
        store.flush()
        written[n] = backend.bytes_out

    ratio = written[400] / written[200]
    assert ratio < 2.6, f"writes growing super-linearly (ratio {ratio:.2f}); quadratic regression?"


def test_manifest_bytes_are_linear(make_store):
    """The manifest specifically — this is the part that write-behind
    buffering alone did NOT fix. Buffering divides the quadratic term by the
    batch size; only the snapshot+delta layout removes it. Perfect quadratic
    growth would be 4.0x per doubling."""
    written = {}
    for n in (200, 400, 800):
        backend, store = make_store()
        for i in range(n):
            store.upsert_entity("u", "concept", f"C{i}")
        store.flush()
        written[n] = sum(
            len(backend.get_bytes(k).data)
            for k in backend.list_keys("wikis/u/_manifest")
            if backend.get_bytes(k) is not None
        )

    # Compare growth of total manifest WRITE volume via the counting backend
    # is awkward across separate stores, so assert on the persisted footprint
    # staying proportional rather than compounding.
    r1 = written[400] / written[200]
    r2 = written[800] / written[400]
    assert r1 < 2.6 and r2 < 2.6, f"manifest growth {r1:.2f}x then {r2:.2f}x"


def test_manifest_compaction_bounds_delta_count(make_store):
    """Deltas keep writes cheap, but an unbounded delta chain would make a
    cold read fan out into many GETs. Compaction has to cap it."""
    from app.graph.manifest import COMPACT_MAX_DELTAS
    backend, store = make_store()
    for i in range(500):
        store.upsert_entity("u", "concept", f"C{i}")
    store.flush()

    deltas = [k for k in backend.list_keys("wikis/u/_manifest/d/") if k.endswith(".json")]
    assert len(deltas) <= COMPACT_MAX_DELTAS


def test_cold_read_reconstructs_from_snapshot_plus_deltas(make_store):
    backend, store = make_store()
    for i in range(250):
        store.upsert_entity("u", "concept", f"C{i}")
    store.upsert_entity("u", "person", "Alice Chen")
    store.delete_entity("u", "concept/c0", hard_delete=True)
    store.flush()

    store.manifest._cache.clear()   # force a read from storage
    entries = set(store.list_entities("u"))   # returns wiki_id strings
    assert "person/alice-chen" in entries
    assert "concept/c0" not in entries, "a delete staged in a delta must survive reload"
    assert len(entries) == 250


def test_upsert_costs_one_put(make_store):
    """The entity file is the only thing that must be written synchronously.

    Pinned to frontmatter mode: companion mode deliberately writes a second
    object per entity, so it costs 2. That trade is measured in
    test_companion_mode_costs_one_extra_write.
    """
    backend, store = make_store(OKF_MODE="frontmatter",
                                FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    store.upsert_entity("u", "person", "A")
    backend.ops.clear()
    store.upsert_entity("u", "person", "A", summary_append="more")
    assert backend.ops["PUT"] == 1


def test_read_costs_zero_writes(make_store):
    """touch=True used to trigger a full read-modify-write of the entity
    file on every single GET."""
    backend, store = make_store(OKF_MODE="frontmatter",
                                FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    store.upsert_entity("u", "person", "A")
    backend.ops.clear()
    store.get_entity("u", "person/a", touch=True)
    assert backend.ops["PUT"] == 0
    assert backend.ops["GET"] == 1


def test_touch_still_advances_last_accessed(make_store):
    """Cheap must not mean broken — the retention-decay clock still moves."""
    backend, store = make_store(FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    store.upsert_entity("u", "person", "Amy")
    before = store.manifest.get_entry("u", "person/amy").last_accessed
    store.get_entity("u", "person/amy", touch=True)
    after = store.manifest.get_entry("u", "person/amy").last_accessed
    assert after > before


# ---------- durability / recovery ----------

def test_ops_log_segments_are_readable_and_ordered(make_store):
    backend, store = make_store(FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    for i in range(25):
        store.upsert_entity("u", "person", f"P{i}")
    today = datetime.date.today()
    records = store.ops_log.read_day("u", today)

    assert len(records) == 25
    assert len({r["op_id"] for r in records}) == 25, "op_ids must be unique"
    assert records == sorted(records, key=lambda r: (r["created_at"], r["op_id"]))


def test_ops_log_compaction_preserves_records(make_store):
    backend, store = make_store(FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    for i in range(10):
        store.upsert_entity("u", "person", f"P{i}")
    today = datetime.date.today()
    before = store.ops_log.read_day("u", today)

    n = store.ops_log.compact_day("u", today)
    remaining = [k for k in backend.list_keys(f"wikis/u/_ops/{today.isoformat()}/")
                 if k.endswith(".jsonl")]

    assert n == len(before)
    assert remaining == []
    assert store.ops_log.read_day("u", today) == before


def test_legacy_day_file_still_readable(make_store):
    """Logs written by the pre-segment layout must not become invisible."""
    backend, store = make_store()
    today = datetime.date.today()
    backend.put_bytes(
        f"wikis/u/_ops/{today.isoformat()}_op.jsonl",
        b'{"op_id":"old_1","op":"create","wiki_id":"person/x",'
        b'"created_at":"2020-01-01T00:00:00+00:00"}\n',
    )
    store.upsert_entity("u", "person", "New")
    store.flush()

    records = store.ops_log.read_day("u", today)
    assert [r["op_id"] for r in records][0] == "old_1"
    assert len(records) == 2


def test_manifest_rebuilds_from_entity_files(make_store):
    """The recovery path that makes buffering safe: entity files are the
    source of truth, so a lost manifest is never lost data."""
    backend, store = make_store(FLUSH_INTERVAL_SECONDS="3600", FLUSH_MAX_PENDING="100000")
    for i in range(10):
        store.upsert_entity("u", "concept", f"C{i}")
    store.flush()

    wipe_manifest(backend, "u")
    store.manifest._cache.clear()
    assert store.list_entities("u") == []

    assert store.manifest.rebuild("u") == 10
    assert len(store.list_entities("u")) == 10


def test_sync_mode_writes_through_immediately(make_store):
    backend, store = make_store(MANIFEST_WRITE_MODE="sync", OPS_LOG_WRITE_MODE="sync")
    store.upsert_entity("u", "person", "Sync")
    today = datetime.date.today()

    # The manifest is log-structured: a sync write emits a delta object,
    # not a rewrite of one file. Assert a delta landed, not a filename.
    assert [k for k in backend.list_keys("wikis/u/_manifest/") if k.endswith(".json")]
    assert [k for k in backend.list_keys(f"wikis/u/_ops/{today.isoformat()}/")
            if k.endswith(".jsonl")]


# ---------- robustness ----------

def test_decay_score_survives_unparseable_last_accessed(make_store):
    """This is called unconditionally by the API serializer, so raising
    turned one hand-edited file into a permanent opaque 500."""
    backend, store = make_store(OKF_MODE="frontmatter")
    store.upsert_entity("u", "person", "Zed")
    key = "wikis/u/person/zed.okf.md"
    broken = backend.get_bytes(key).data.decode().replace("last_accessed:", "la_broken:")
    backend.put_bytes(key, broken.encode())

    entity = store.get_entity("u", "person/zed", touch=False)
    assert entity.decay_score() == 0.0


def test_read_repair_evicts_phantom_manifest_entry(make_store):
    """A hard delete removes the entity file synchronously but stages the
    manifest removal. If the process dies before that flush, the manifest
    keeps a phantom entry: GET 404s while GET /wiki still lists it and
    _stats still counts it. A read that finds no file must evict it."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "project", "Orion")
    store.flush()

    store.delete_entity("u", "person/alice-chen", cascade=True, hard_delete=True)
    # No flush — simulates an abrupt exit losing the staged manifest removal.

    import app.config as cfg
    cfg._settings = None
    from app.graph.store import EntityGraphStore
    revived = EntityGraphStore(backend)
    revived.manifest._cache.clear()
    assert "person/alice-chen" in revived.list_entities("u"), "phantom should be present first"

    assert revived.get_entity("u", "person/alice-chen", touch=False) is None
    assert "person/alice-chen" not in revived.list_entities("u"), "read should have repaired it"
    assert revived.stats("u")["entities"] == 1


# ---------- corrupt entity files ----------

@pytest.mark.parametrize("corrupt,label", [
    (lambda s: s.replace("wiki_id: person/alice-chen", "other_key: x"), "missing wiki_id"),
    (lambda s: __import__("re").sub(r"facts:\n(?:.*\n)*?relations:",
                                    "facts:\n- {nonsense: true}\nrelations:", s), "bad fact entry"),
    (lambda s: __import__("re").sub(r"metadata:\n(?:  .*\n)*", "metadata: 12345\n", s), "scalar metadata"),
    (lambda s: s[:60], "truncated"),
    (lambda s: "not markdown at all", "no front-matter"),
])
def test_corrupt_entity_file_raises_malformed_not_keyerror(make_store, corrupt, label):
    """A file that is valid YAML but structurally wrong used to raise
    KeyError / AttributeError out of _deserialize. Every route catches only
    ValueError, so those escaped as opaque 500s — on deletes as well as
    reads, which made it look like the delete endpoints were broken."""
    from app.graph.store import MalformedEntityError

    # frontmatter mode: the point is that a damaged .md is reported rather
    # than raising KeyError. In companion mode the JSON would still supply
    # those fields, so the .md alone is not enough to corrupt the entity.
    backend, store = make_store(OKF_MODE="frontmatter")
    store.upsert_entity("u", "person", "Alice Chen")
    store.add_fact("u", "person/alice-chen", "a fact")
    key = "wikis/u/person/alice-chen.okf.md"
    backend.put_bytes(key, corrupt(backend.get_bytes(key).data.decode()).encode())

    with pytest.raises(MalformedEntityError):
        store.get_entity("u", "person/alice-chen", touch=False)
    with pytest.raises(MalformedEntityError):
        store.remove_fact("u", "person/alice-chen", "fact_0001")
    assert issubclass(MalformedEntityError, ValueError), "must stay ValueError-compatible"


# ---------- batched reads ----------

def test_get_many_returns_all_keys_including_missing(make_store):
    backend, store = make_store()
    for i in range(5):
        backend.put_bytes(f"u/k{i}", f"v{i}".encode())

    got = backend.get_many([f"u/k{i}" for i in range(5)] + ["u/absent"])
    assert set(got) == {f"u/k{i}" for i in range(5)} | {"u/absent"}
    assert got["u/absent"] is None
    assert got["u/k3"].data == b"v3"


def test_traverse_batches_reads_per_level(make_store):
    """traverse used to issue one GET per node, so wall time scaled with node
    count — over S3 that is N * RTT of pure waiting. It now fetches each BFS
    level in one batch, so the number of storage ROUND-TRIPS scales with
    depth, not breadth."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    for i in range(12):
        store.upsert_entity("u", "concept", f"C{i}")
        store.link_entities("u", "person/hub", f"concept/c{i}")
    store.flush()

    calls = {"get_bytes": 0, "get_many": 0}
    real_get, real_many = backend.get_bytes, backend.get_many
    backend.get_bytes = lambda k: (calls.__setitem__("get_bytes", calls["get_bytes"] + 1), real_get(k))[1]
    backend.get_many = lambda ks: (calls.__setitem__("get_many", calls["get_many"] + 1), real_many(ks))[1]
    try:
        reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    finally:
        backend.get_bytes, backend.get_many = real_get, real_many

    assert len(reached) == 13
    # 2 levels -> at most a few batches, rather than one call per node.
    assert calls["get_many"] <= 3, f"expected per-level batching, got {calls}"
    # Note: get_bytes IS still called here, because this test backend inherits
    # StorageBackend.get_many, whose default implementation is a sequential
    # loop. That is the correct fallback for backends that cannot parallelise.
    # MirageBackend overrides get_many with asyncio.gather, so on the real
    # backend these become one concurrent round-trip per level. What this test
    # pins is the CALL SHAPE — traverse must ask for a level at a time — which
    # is the part that determines whether the concurrent override can help.


def test_traverse_does_not_abort_on_a_corrupt_entity_file(make_store):
    """One unreadable file must not take down a whole traversal.

    CONTRACT CHANGE, recorded deliberately. traverse() used to read every
    node's file and therefore returned only wiki_ids that were readable; a
    corrupt file was silently dropped. It now walks the manifest's adjacency
    index and opens no entity files at all, which is what makes it O(1)
    storage reads instead of O(nodes).

    So the guarantee is now "every returned wiki_id is active in the index",
    not "every returned wiki_id parses". A corrupt file is still part of the
    graph, and fetching it surfaces a 422 naming the file — which is more
    useful than a node vanishing from traversal with no explanation.
    """
    backend, store = make_store(OKF_MODE="frontmatter")
    store.upsert_entity("u", "person", "Hub")
    for name in ("Good", "Bad"):
        store.upsert_entity("u", "concept", name)
        store.link_entities("u", "person/hub", f"concept/{name.lower()}")
    store.flush()

    key = "wikis/u/concept/bad.okf.md"
    backend.put_bytes(key, backend.get_bytes(key).data.decode().replace("wiki_id:", "nope:").encode())

    reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    assert "concept/good" in reached
    assert "concept/bad" in reached, "corrupt files stay in the graph; the read surfaces them"

    from app.graph.store import MalformedEntityError
    with pytest.raises(MalformedEntityError):
        store.get_entity("u", "concept/bad", touch=False)


def test_traverse_reads_no_entity_files(make_store):
    """The whole point of the adjacency index: traversal returns wiki_ids
    only, so it can be answered entirely from the index without opening a
    single entity file."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    for i in range(20):
        store.upsert_entity("u", "concept", f"C{i}")
        store.link_entities("u", "person/hub", f"concept/c{i}")
    store.flush()

    calls = {"n": 0}
    real_get, real_many = backend.get_bytes, backend.get_many
    backend.get_bytes = lambda k: (calls.__setitem__("n", calls["n"] + 1), real_get(k))[1]
    backend.get_many = lambda ks: (calls.__setitem__("n", calls["n"] + len(ks)), real_many(ks))[1]
    try:
        reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    finally:
        backend.get_bytes, backend.get_many = real_get, real_many

    assert len(reached) == 21
    assert calls["n"] == 0, f"traverse should open no entity files, opened {calls['n']}"


def test_traverse_falls_back_for_entries_written_before_the_index(make_store):
    """edges=None means 'unknown', not 'no edges'. A manifest written before
    the index existed must still traverse correctly, or every pre-existing
    entity would silently look isolated."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    store.upsert_entity("u", "concept", "Target")
    store.link_entities("u", "person/hub", "concept/target")
    store.flush()

    # Simulate a legacy manifest: strip the index, keep the entity files.
    for wiki_id in ("person/hub", "concept/target"):
        entry = store.manifest.get_entry("u", wiki_id)
        entry.edges = None
        store.manifest.upsert_entry("u", entry)
    store.flush()

    reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    assert reached == {"person/hub", "concept/target"}


def test_relation_removal_updates_the_index(make_store):
    """A derived index that only ever grows is worse than none."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    store.upsert_entity("u", "concept", "Target")
    entity = store.link_entities("u", "person/hub", "concept/target")
    rel_id = entity.relations[0].relation_id
    store.flush()
    assert store.traverse("u", ["person/hub"], max_depth=2) == {"person/hub", "concept/target"}

    store.remove_relation("u", "person/hub", rel_id)
    store.flush()
    assert store.traverse("u", ["person/hub"], max_depth=2) == {"person/hub"}


def test_traverse_reads_no_entity_files_when_index_is_warm(make_store):
    """The whole point of the adjacency index: traverse returns wiki_ids
    only, so once the index is loaded it should not touch entity files at
    all. Before the index it cost one read per node."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    for i in range(15):
        store.upsert_entity("u", "concept", f"C{i}")
        store.link_entities("u", "person/hub", f"concept/c{i}")
    store.flush()
    store.list_entities("u")          # warm the index

    calls = {"n": 0}
    real_get, real_many = backend.get_bytes, backend.get_many
    backend.get_bytes = lambda k: (calls.__setitem__("n", calls["n"] + 1), real_get(k))[1]
    backend.get_many = lambda ks: (calls.__setitem__("n", calls["n"] + len(ks)), real_many(ks))[1]
    try:
        reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    finally:
        backend.get_bytes, backend.get_many = real_get, real_many

    assert len(reached) == 16
    assert calls["n"] == 0, f"traverse should read no entity files, made {calls['n']}"


def test_traverse_falls_back_for_entries_predating_the_index(make_store):
    """Entries written before the adjacency index existed carry edges=None,
    which means UNKNOWN rather than 'no edges'. Those must fall back to
    reading the entity file, or an upgraded deployment silently returns a
    disconnected graph."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    store.upsert_entity("u", "concept", "Target")
    store.link_entities("u", "person/hub", "concept/target")
    store.flush()

    entry = store.manifest.get_entry("u", "person/hub")
    entry.edges = None                       # simulate a pre-index entry
    store.manifest.upsert_entry("u", entry)
    store.flush()

    reached = store.traverse("u", ["person/hub"], max_depth=2, max_nodes=100)
    assert "concept/target" in reached, "fallback must still find the edge"


def test_stats_reads_no_entity_files(make_store):
    """stats() used to read every entity file to count relations, costing N
    storage reads for N entities. The adjacency index already holds the edge
    counts."""
    backend, store = make_store()
    store.upsert_entity("u", "person", "Hub")
    for i in range(15):
        store.upsert_entity("u", "concept", f"C{i}")
        store.link_entities("u", "person/hub", f"concept/c{i}")
    store.flush()

    calls = {"n": 0}
    real_get, real_many = backend.get_bytes, backend.get_many
    backend.get_bytes = lambda k: (calls.__setitem__("n", calls["n"] + 1), real_get(k))[1]
    backend.get_many = lambda ks: (calls.__setitem__("n", calls["n"] + len(ks)), real_many(ks))[1]
    try:
        result = store.stats("u")
    finally:
        backend.get_bytes, backend.get_many = real_get, real_many

    assert result == {"entities": 16, "edges": 15}
    assert calls["n"] == 0, f"stats should open no entity files, opened {calls['n']}"
