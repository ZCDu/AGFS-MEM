"""
Optional semantic-search fallback: local embeddings via BAAI's bge-m3 model,
run in-process (no separate service, no Docker, no per-query API cost).

WHY THIS EXISTS
    Lexical matching (app/graph/lexical.py) catches "same words, different
    phrasing". It cannot bridge conceptually-related but textually-unrelated
    terms -- a Chinese query for the word "department" sharing zero
    characters with an entity about a "branch" scores 0 no matter how the
    tokens are sliced. Only a meaning-level comparison closes that gap.

WHY IN-PROCESS, NOT A SEPARATE SERVICE
    A standalone server (Ollama, llama.cpp) is one more thing to install,
    run, and keep alive -- and in some networks (e.g. mainland China without
    a working mirror), getting one running at all is its own project. A
    Python library loaded directly into this app has none of that: nothing
    to install on any client, nothing that can crash independently, no port.
    Model weights (~1-2GB) download once (via ModelScope, not the default
    Hugging Face hub, since that's what's actually reachable from mainland
    China) and are cached to local disk; every embed call after that is pure
    local CPU inference. Zero network calls, zero per-query cost, once the
    model is warm.

DEGRADES, DOES NOT CRASH
    Same posture as app/extract/llm.py: nothing here is required for the
    rest of the service to work. `EMBEDDING_ENABLED` defaults to false, and
    even with it set, a missing package or a failed model load makes
    embed() return None rather than raise -- callers fall back to
    lexical-only search, exactly as if the feature were never turned on.

COST CONTROL -- THIS IS A FALLBACK TIER, NOT A REPLACEMENT
    embed() is deliberately not called on every search. app/graph/search.py
    and app/verify/assessor.py only reach for it when lexical matching finds
    nothing at all, so the (comparatively) expensive step stays rare instead
    of running on every query.

STORAGE -- WHY A SEPARATE INDEX, NOT A FIELD ON ManifestEntry
    WikiManifest's snapshot compaction fully materializes every entry into
    memory and rewrites the whole snapshot in one json.dumps (see
    app/graph/manifest.py). A ~1024-float vector per entity is several KB of
    JSON -- adding it to ManifestEntry would multiply that rewrite cost
    across the whole manifest on every compaction. EmbeddingIndex below is
    the same snapshot + delta-chain design as WikiManifest, for the same
    reason (a single mutable object rewritten on every write costs O(N^2)
    total bytes as the wiki grows -- see manifest.py's module docstring),
    but kept in its own small store under wikis/{scope}/_embeddings/ so the
    hot manifest stays hot. Entity files remain the source of truth, exactly
    as manifest.py's docstring already establishes for the catalogue index --
    EmbeddingIndex is equally derived and rebuildable (re-embed compact
    fields), never authoritative.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone

from app.graph.keys import wiki_key
from app.storage.backend import StorageBackend
from app.storage.writebehind import FlushBuffer

logger = logging.getLogger("memory_backend.embeddings")

COMPACT_RATIO = 0.5
COMPACT_MAX_DELTAS = 20

_model = None
_model_lock = threading.Lock()


def _get_model():
    """Lazy singleton: loaded on first real use, never at import time and
    never per-request. A 1-2GB model load is a one-time process cost, not
    something any caller should risk triggering twice.

    FIRST-RUN DOWNLOAD: FlagEmbedding pulls bge-m3 from the Hugging Face hub
    by default, which is unreachable from some networks (e.g. mainland China
    without a mirror). If that's your deployment, pre-download the model
    ONCE via ModelScope before turning EMBEDDING_ENABLED on:
        pip install modelscope
        modelscope download BAAI/bge-m3 --local_dir ~/.cache/huggingface/hub/models--BAAI--bge-m3
    That's an operator setup step, not something this module automates --
    it only needs to happen once per machine, and the model is fully local
    (no network calls at all) after that regardless of which path it came
    from."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from FlagEmbedding import BGEM3FlagModel
                _model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
    return _model


def is_embedding_enabled() -> bool:
    """Feature flag: off by default, and turns itself off if the optional
    dependency isn't installed. Deliberately NOT cached at module scope --
    it's cheap to recompute (a settings lookup plus an import, which Python
    itself caches in sys.modules on success), and a persistent global here
    would go stale exactly the way settings caching bugs already have
    elsewhere in this codebase's test suite."""
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception:
        return False
    if not settings.embedding_enabled:
        return False
    try:
        import FlagEmbedding  # noqa: F401
        return True
    except Exception:
        logger.warning(
            "EMBEDDING_ENABLED=true but the FlagEmbedding package is not "
            "installed; semantic search stays off, lexical search is "
            "unaffected. `pip install FlagEmbedding` to enable it.")
        return False


def embed(text: str) -> list[float] | None:
    """None (not a raised exception) means "no semantic signal available" --
    disabled, not-yet-downloaded, or a runtime failure all degrade the same
    way: the caller proceeds with lexical-only results."""
    if not text or not is_embedding_enabled():
        return None
    try:
        model = _get_model()
        return model.encode([text])["dense_vecs"][0].tolist()
    except Exception:
        logger.warning("embedding failed, falling back to lexical-only search",
                       exc_info=True)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Pure Python, no numpy -- consistent with app/graph/lexical.py, and
    this only ever runs over a handful of short vectors already in memory
    per query (see EmbeddingIndex), not a bulk numerical workload."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingIndex(FlushBuffer):
    """Per-scope wiki_id -> embedding vector, persisted as a snapshot plus a
    delta chain, exactly like WikiManifest (app/graph/manifest.py) and for
    the identical reason: rewriting one growing object on every write costs
    O(N^2) total bytes over the life of the index. See that module's
    docstring for the full cost argument; this is the same design applied to
    vectors instead of catalogue metadata, deliberately NOT shared code with
    WikiManifest since that class carries a lot of manifest-only shape
    (aliases, edges, layout coordinates, legacy-file fallback) this index
    has no use for.

    Fully derived and rebuildable: an entity's vector is recomputed from its
    `compact` field on every write, so losing the index (or a whole scope's
    embeddings) never loses data, only the semantic-fallback signal until
    entities are re-embedded.
    """

    _seq = 0
    _pid = os.getpid()

    def __init__(self, backend: StorageBackend, mode: str = "buffered",
                 flush_interval: float = 2.0, max_pending: int = 100):
        super().__init__(flush_interval=flush_interval, max_pending=max_pending)
        self.backend = backend
        self.mode = mode
        self._cache: dict[str, dict[str, list[float]]] = {}
        self._staged: dict[str, dict] = {}
        self._delta_keys: dict[str, list[str]] = {}
        self._delta_bytes: dict[str, int] = {}
        self._snap_bytes: dict[str, int] = {}
        self._seq_lock = threading.Lock()

    # ---------- keys ----------

    def _snapshot_key(self, scope: str) -> str:
        return wiki_key(scope, "_embeddings/snapshot.json")

    def _delta_prefix(self, scope: str) -> str:
        return wiki_key(scope, "_embeddings/d") + "/"

    def _next_seq(self) -> str:
        with self._seq_lock:
            EmbeddingIndex._seq += 1
            n = EmbeddingIndex._seq
        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{self._pid}-{n:06d}"

    # ---------- storage-level read ----------

    def _load_from_storage(self, scope: str) -> dict:
        data: dict = {}
        snap = self.backend.get_bytes(self._snapshot_key(scope))
        if snap is not None:
            data = json.loads(snap.data.decode("utf-8")).get("vectors", {})
            self._snap_bytes[scope] = len(snap.data)
        self._snap_bytes.setdefault(scope, 0)

        keys = sorted(k for k in self.backend.list_keys(self._delta_prefix(scope))
                      if k.endswith(".json"))
        total = 0
        for key in keys:
            raw = self.backend.get_bytes(key)
            if raw is None:
                continue
            total += len(raw.data)
            delta = json.loads(raw.data.decode("utf-8"))
            for wid in delta.get("remove", []):
                data.pop(wid, None)
            data.update(delta.get("upsert", {}))
        self._delta_keys[scope] = keys
        self._delta_bytes[scope] = total
        return data

    def _load_locked(self, scope: str) -> dict:
        if scope not in self._cache:
            self._cache[scope] = self._load_from_storage(scope)
        return self._cache[scope]

    # ---------- storage-level write ----------

    def _write_delta(self, scope: str, pending: dict) -> None:
        payload = json.dumps(
            {"upsert": pending["upsert"], "remove": sorted(pending["remove"])},
        ).encode("utf-8")
        key = f"{self._delta_prefix(scope)}{self._next_seq()}.json"
        self.backend.put_bytes(key, payload)
        self._delta_keys.setdefault(scope, []).append(key)
        self._delta_bytes[scope] = self._delta_bytes.get(scope, 0) + len(payload)

    def _should_compact(self, scope: str) -> bool:
        snap = self._snap_bytes.get(scope, 0)
        deltas = self._delta_bytes.get(scope, 0)
        n = len(self._delta_keys.get(scope, []))
        if n >= COMPACT_MAX_DELTAS:
            return True
        return snap > 0 and deltas >= snap * COMPACT_RATIO

    def _compact_locked(self, scope: str) -> None:
        data = self._cache.get(scope, {})
        payload = json.dumps({"vectors": data}).encode("utf-8")
        self.backend.put_bytes(self._snapshot_key(scope), payload)
        self._snap_bytes[scope] = len(payload)
        for key in self._delta_keys.get(scope, []):
            try:
                self.backend.delete(key)
            except Exception:
                pass
        self._delta_keys[scope] = []
        self._delta_bytes[scope] = 0

    def _flush_locked(self) -> None:
        for scope, pending in list(self._staged.items()):
            if not pending["upsert"] and not pending["remove"]:
                continue
            self._write_delta(scope, pending)
            self._staged[scope] = {"upsert": {}, "remove": set()}
            if self._should_compact(scope):
                self._compact_locked(scope)
        self._staged = {s: p for s, p in self._staged.items()
                        if p["upsert"] or p["remove"]}

    # ---------- mutation plumbing ----------

    def _stage(self, scope: str, upserts: dict, removes: set) -> None:
        with self._lock:
            data = self._load_locked(scope)
            for wid in removes:
                data.pop(wid, None)
            data.update(upserts)

            p = self._staged.setdefault(scope, {"upsert": {}, "remove": set()})
            for wid in removes:
                p["upsert"].pop(wid, None)
                p["remove"].add(wid)
            for wid, vec in upserts.items():
                p["remove"].discard(wid)
                p["upsert"][wid] = vec

            if self.mode == "sync":
                self._flush_locked()
            else:
                self._mark_dirty()

    def _snapshot(self, scope: str) -> dict:
        with self._lock:
            if self.mode == "sync":
                self._cache.pop(scope, None)
            return dict(self._load_locked(scope))

    # ---------- public API ----------

    def upsert(self, scope: str, wiki_id: str, vector: list[float]) -> None:
        self._stage(scope, {wiki_id: vector}, set())

    def remove(self, scope: str, wiki_id: str) -> None:
        self._stage(scope, {}, {wiki_id})

    def get_all(self, scope: str) -> dict[str, list[float]]:
        """Every embedded entity's vector for this scope, already in memory
        after the first call -- comparing a query vector against these costs
        zero storage reads."""
        return self._snapshot(scope)
