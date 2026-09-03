"""
Tests for app/graph/embeddings.py.

Never loads the real bge-m3 model (a 1-2GB download with no place in a test
suite) -- EmbeddingIndex tests use plain fake vectors, and embed()/
is_embedding_enabled() are exercised only for their degrade-gracefully
behavior (off by default, off when the optional package isn't installed).
"""

from __future__ import annotations

import pytest

from app.graph.embeddings import EmbeddingIndex, cosine, is_embedding_enabled
from app.storage.mirage_backend import MirageBackend


# ---------- cosine() ----------

def test_cosine_identical_vectors_is_one():
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_negative_one():
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_empty_vectors_is_zero_not_an_error():
    assert cosine([], [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------- is_embedding_enabled() ----------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EMBEDDING_ENABLED", raising=False)
    import app.config as config_module
    config_module._settings = None
    assert is_embedding_enabled() is False


def test_stays_disabled_without_the_optional_package(monkeypatch):
    """EMBEDDING_ENABLED=true alone must not be enough if FlagEmbedding
    isn't installed -- the whole point is that this never crashes a
    deployment that hasn't opted into the extra dependency."""
    monkeypatch.setenv("EMBEDDING_ENABLED", "true")
    import app.config as config_module
    config_module._settings = None
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "FlagEmbedding":
            raise ImportError("not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert is_embedding_enabled() is False


# ---------- EmbeddingIndex ----------

@pytest.fixture()
def backend(tmp_path):
    b = MirageBackend.from_disk(root=str(tmp_path / "bucket"))
    yield b
    b.close()


@pytest.fixture()
def index(backend):
    return EmbeddingIndex(backend, mode="sync")


def test_upsert_and_get_all(index):
    index.upsert("wiki-a", "person/alice", [1.0, 0.0, 0.0])
    index.upsert("wiki-a", "project/orion", [0.0, 1.0, 0.0])
    vectors = index.get_all("wiki-a")
    assert vectors == {
        "person/alice": [1.0, 0.0, 0.0],
        "project/orion": [0.0, 1.0, 0.0],
    }


def test_scopes_are_independent(index):
    index.upsert("wiki-a", "person/alice", [1.0, 0.0])
    index.upsert("wiki-b", "person/bob", [0.0, 1.0])
    assert list(index.get_all("wiki-a").keys()) == ["person/alice"]
    assert list(index.get_all("wiki-b").keys()) == ["person/bob"]


def test_remove(index):
    index.upsert("wiki-a", "person/alice", [1.0, 0.0])
    index.remove("wiki-a", "person/alice")
    assert index.get_all("wiki-a") == {}


def test_upsert_overwrites_existing_vector(index):
    index.upsert("wiki-a", "person/alice", [1.0, 0.0])
    index.upsert("wiki-a", "person/alice", [0.0, 1.0])
    assert index.get_all("wiki-a") == {"person/alice": [0.0, 1.0]}


def test_persists_across_a_fresh_index_instance(backend):
    """The whole point of the snapshot+delta design: a new process (a new
    EmbeddingIndex over the same backend) must see what a prior one wrote,
    not just serve from an in-memory cache that dies with the object."""
    first = EmbeddingIndex(backend, mode="sync")
    first.upsert("wiki-a", "person/alice", [1.0, 2.0, 3.0])

    second = EmbeddingIndex(backend, mode="sync")
    assert second.get_all("wiki-a") == {"person/alice": [1.0, 2.0, 3.0]}


def test_buffered_mode_flushes_on_close(backend):
    """Default (buffered) mode must not lose writes on a clean shutdown --
    same durability contract WikiManifest already gives."""
    buffered = EmbeddingIndex(backend, mode="buffered")
    buffered.upsert("wiki-a", "person/alice", [1.0, 0.0])
    buffered.close()

    reader = EmbeddingIndex(backend, mode="sync")
    assert reader.get_all("wiki-a") == {"person/alice": [1.0, 0.0]}


def test_compaction_folds_deltas_without_losing_data(backend):
    """Force enough writes to trigger COMPACT_MAX_DELTAS and confirm the
    folded snapshot still reads back everything correctly."""
    from app.graph import embeddings as embeddings_module
    monkeypatch_value = embeddings_module.COMPACT_MAX_DELTAS
    embeddings_module.COMPACT_MAX_DELTAS = 3
    try:
        idx = EmbeddingIndex(backend, mode="sync")
        for i in range(10):
            idx.upsert("wiki-a", f"concept/item-{i}", [float(i), 0.0])
        vectors = idx.get_all("wiki-a")
        assert len(vectors) == 10
        assert vectors["concept/item-7"] == [7.0, 0.0]

        # A fresh instance must read the same state back from storage.
        fresh = EmbeddingIndex(backend, mode="sync")
        assert fresh.get_all("wiki-a") == vectors
    finally:
        embeddings_module.COMPACT_MAX_DELTAS = monkeypatch_value
