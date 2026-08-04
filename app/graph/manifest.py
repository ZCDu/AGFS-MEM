"""
Wiki manifest — the per-user index mapping wiki_id -> a small ManifestEntry.
Two jobs:

  1. Avoids a directory scan (`list_keys`) every time you want to list or
     look up entities — one read instead of N.
  2. Doubles as the "compact view" cache from PLAN.md §9.3. The plan wants
     Redis for this; this program has no Redis in scope, so the manifest
     is the substitute.

STORAGE LAYOUT — log-structured, not one mutable file
----------------------------------------------------
Object stores have no append and no partial update: changing one byte of a
file means re-uploading the whole file. A single `_manifest.json` rewritten
on every change therefore costs O(size) per write, so writing N entities
costs O(N^2) bytes in total.

Write-behind buffering alone does NOT fix this. Batching K changes into one
rewrite divides the cost by K, but the total is still O(N^2 / K) — it is
the same curve with a smaller constant. Measured on this codebase at the
default batch size, manifest bytes grew ~3.6x per doubling of entity count
(4.0x would be perfectly quadratic).

So the manifest is stored the way a log-structured merge tree stores its
levels — an immutable base plus a chain of immutable deltas:

    {user_id}/wiki/_manifest/snapshot.json     full state as of some point
    {user_id}/wiki/_manifest/d/{seq}.json      changes since the snapshot

A write appends one small delta object whose size is proportional to the
number of CHANGED entries, not to the size of the whole index. Reading
means snapshot + deltas applied in sequence order.

Deltas are folded back into a new snapshot when they grow past
COMPACT_RATIO of the snapshot's size, or exceed COMPACT_MAX_DELTAS objects
(which bounds read cost). Because the trigger is proportional to current
size, each entry is rewritten O(log N) times over the life of the index
rather than O(N) times — total write volume O(N log N) instead of O(N^2).

The legacy single `_manifest.json` is still read as a snapshot if present,
so indexes written by the previous layout keep working.

WRITE MODES (MANIFEST_WRITE_MODE)
---------------------------------
`buffered` — (default) hold changes in memory, emit one delta per flush.
`sync`     — emit a delta per mutation, before the call returns. Correct
             across multiple processes; more requests.

Buffering is safe here because the manifest is DERIVED state: entity files
are the source of truth (ADR-001) and `rebuild()` reconstructs the index
from them. See app/storage/writebehind.py for the durability tradeoff and
the multi-process caveat.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.storage.backend import ConflictError, StorageBackend
from app.storage.writebehind import FlushBuffer

MAX_WRITE_RETRIES = 5

# Compact once the deltas are worth half the snapshot. Lower = more frequent
# compaction (cheaper reads, more write volume); higher = the reverse.
COMPACT_RATIO = 0.5
# Hard cap on delta objects, so a cold read never fans out into many GETs
# even when each delta is tiny.
COMPACT_MAX_DELTAS = 20


@dataclass
class ManifestEntry:
    wiki_id: str
    type: str
    title: str
    aliases: list[str] = field(default_factory=list)
    path: str = ""
    compact: str = ""
    status: str = "stable"
    updated_at: str = ""
    last_accessed: str = ""
    # Adjacency index: this entity's outbound edges, as compact
    # {"t": target_wiki_id, "c": category} records.
    #
    # Relations live inside the entity file, which is still the source of
    # truth. Mirroring them here turns graph traversal from "read one object
    # per node" into "read the index once" — measured at 31 storage
    # operations versus 1 for a depth-2 walk over 31 nodes. Since traverse()
    # returns only wiki_ids and never entity content, the index answers the
    # whole query and the entity files never need to be opened.
    #
    # None and [] mean different things and the distinction matters:
    #   None -> this entry predates the index; edges are UNKNOWN, so callers
    #           must fall back to reading the entity file.
    #   []   -> indexed, and this entity genuinely has no outbound edges.
    # Without that split, every manifest written before this feature would
    # silently look like an isolated node.
    edges: list[dict] | None = None
    # Persisted layout coordinates from the graph editor. Purely
    # presentational, and optional — but persisting them means the force
    # simulation runs once rather than on every page load, and a node stays
    # where you left it between sessions, which matters more for usability
    # than frame rate does.
    x: float | None = None
    y: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ManifestEntry":
        return ManifestEntry(
            wiki_id=d["wiki_id"],
            type=d.get("type", ""),
            title=d.get("title", d["wiki_id"]),
            aliases=list(d.get("aliases", [])),
            path=d.get("path", ""),
            compact=d.get("compact", ""),
            status=d.get("status", "stable"),
            updated_at=d.get("updated_at", ""),
            last_accessed=d.get("last_accessed", ""),
            edges=d.get("edges"),
            x=d.get("x"), y=d.get("y"),
        )


class WikiManifest(FlushBuffer):
    _seq = 0
    _pid = os.getpid()

    def __init__(self, backend: StorageBackend, mode: str = "buffered",
                 flush_interval: float = 2.0, max_pending: int = 100):
        super().__init__(flush_interval=flush_interval, max_pending=max_pending)
        self.backend = backend
        self.mode = mode
        self._cache: dict[str, dict] = {}          # user -> merged {wiki_id: entry}
        self._staged: dict[str, dict] = {}        # user -> {"upsert": {...}, "remove": set()}
        self._delta_keys: dict[str, list[str]] = {}  # user -> delta object keys seen
        self._delta_bytes: dict[str, int] = {}
        self._snap_bytes: dict[str, int] = {}
        self._seq_lock = threading.Lock()

    # ---------- keys ----------

    def _legacy_key(self, user_id: str) -> str:
        return f"{user_id}/wiki/_manifest.json"

    def _snapshot_key(self, user_id: str) -> str:
        return f"{user_id}/wiki/_manifest/snapshot.json"

    def _delta_prefix(self, user_id: str) -> str:
        return f"{user_id}/wiki/_manifest/d/"

    def _next_seq(self) -> str:
        with self._seq_lock:
            WikiManifest._seq += 1
            n = WikiManifest._seq
        # Lexicographic order must match time order, since deltas are applied
        # in key order and later writes must win.
        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{self._pid}-{n:06d}"

    # ---------- storage-level read ----------

    def _load_from_storage(self, user_id: str) -> dict:
        """snapshot (or legacy file) + every delta, applied in order."""
        data: dict = {}
        snap = self.backend.get_bytes(self._snapshot_key(user_id))
        if snap is None:
            snap = self.backend.get_bytes(self._legacy_key(user_id))
            if snap is not None:
                # Legacy layout: the whole file is the entry map.
                data = json.loads(snap.data.decode("utf-8"))
                self._snap_bytes[user_id] = len(snap.data)
        else:
            data = json.loads(snap.data.decode("utf-8")).get("entries", {})
            self._snap_bytes[user_id] = len(snap.data)
        self._snap_bytes.setdefault(user_id, 0)

        keys = sorted(k for k in self.backend.list_keys(self._delta_prefix(user_id))
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
        self._delta_keys[user_id] = keys
        self._delta_bytes[user_id] = total
        return data

    def _load_locked(self, user_id: str) -> dict:
        if user_id not in self._cache:
            self._cache[user_id] = self._load_from_storage(user_id)
        return self._cache[user_id]

    # ---------- storage-level write ----------

    def _write_delta(self, user_id: str, pending: dict) -> None:
        """One PUT whose size tracks the number of CHANGED entries — not the
        size of the whole index. This is the property that removes the
        quadratic term."""
        payload = json.dumps(
            {"upsert": pending["upsert"], "remove": sorted(pending["remove"])},
            ensure_ascii=False,
        ).encode("utf-8")
        key = f"{self._delta_prefix(user_id)}{self._next_seq()}.json"
        self.backend.put_bytes(key, payload)
        self._delta_keys.setdefault(user_id, []).append(key)
        self._delta_bytes[user_id] = self._delta_bytes.get(user_id, 0) + len(payload)

    def _should_compact(self, user_id: str) -> bool:
        snap = self._snap_bytes.get(user_id, 0)
        deltas = self._delta_bytes.get(user_id, 0)
        n = len(self._delta_keys.get(user_id, []))
        if n >= COMPACT_MAX_DELTAS:
            return True
        # Triggering proportionally to current size is what bounds each
        # entry to O(log N) rewrites instead of O(N).
        return snap > 0 and deltas >= snap * COMPACT_RATIO

    def _compact_locked(self, user_id: str) -> None:
        data = self._cache.get(user_id, {})
        payload = json.dumps({"entries": data}, ensure_ascii=False).encode("utf-8")
        self.backend.put_bytes(self._snapshot_key(user_id), payload)
        self._snap_bytes[user_id] = len(payload)
        # Only delete the deltas already folded in. A delta written
        # concurrently by another process after this list was captured
        # survives and is picked up on the next read.
        for key in self._delta_keys.get(user_id, []):
            try:
                self.backend.delete(key)
            except Exception:
                pass
        self._delta_keys[user_id] = []
        self._delta_bytes[user_id] = 0

    def _flush_locked(self) -> None:
        for user_id, pending in list(self._staged.items()):
            if not pending["upsert"] and not pending["remove"]:
                continue
            self._write_delta(user_id, pending)
            self._staged[user_id] = {"upsert": {}, "remove": set()}
            if self._should_compact(user_id):
                self._compact_locked(user_id)
        self._staged = {u: p for u, p in self._staged.items()
                         if p["upsert"] or p["remove"]}

    # ---------- mutation plumbing ----------

    def _stage(self, user_id: str, upserts: dict, removes: set) -> None:
        with self._lock:
            data = self._load_locked(user_id)
            for wid in removes:
                data.pop(wid, None)
            data.update(upserts)

            p = self._staged.setdefault(user_id, {"upsert": {}, "remove": set()})
            for wid in removes:
                p["upsert"].pop(wid, None)
                p["remove"].add(wid)
            for wid, entry in upserts.items():
                p["remove"].discard(wid)
                p["upsert"][wid] = entry

            if self.mode == "sync":
                # Call _flush_locked directly rather than _flush_now_locked:
                # the latter short-circuits on FlushBuffer's dirty counter,
                # which sync mode never increments. Errors propagate here by
                # design — sync exists so callers can rely on the write
                # having landed, so silently swallowing a failure (which is
                # correct for buffered mode) would defeat the point.
                self._flush_locked()
            else:
                self._mark_dirty()

    def _snapshot(self, user_id: str) -> dict:
        with self._lock:
            if self.mode == "sync":
                # Don't serve a stale cache when another process may have
                # appended deltas since we last looked.
                self._cache.pop(user_id, None)
            return dict(self._load_locked(user_id))

    # ---------- public API ----------

    def upsert_entry(self, user_id: str, entry: ManifestEntry) -> None:
        with self._lock:
            existing = self._load_locked(user_id).get(entry.wiki_id)
        d = entry.to_dict()
        # Preserve a live last_accessed that touch() may have advanced past
        # whatever the entity write carried.
        if existing and existing.get("last_accessed", "") > d.get("last_accessed", ""):
            d["last_accessed"] = existing["last_accessed"]
        # Layout position is written by a different caller than entity writes,
        # so an entity update must not blank it.
        if existing and d.get("x") is None and d.get("y") is None:
            d["x"], d["y"] = existing.get("x"), existing.get("y")
        self._stage(user_id, {entry.wiki_id: d}, set())

    def remove_entry(self, user_id: str, wiki_id: str) -> None:
        self._stage(user_id, {}, {wiki_id})

    def touch(self, user_id: str, wiki_id: str, when: str) -> None:
        """Record a read. In buffered mode this costs no storage write at
        all until the next flush, and many reads of the same entity collapse
        into one delta line."""
        with self._lock:
            entry = self._load_locked(user_id).get(wiki_id)
            if entry is None:
                return
            updated = dict(entry)
            updated["last_accessed"] = when
        self._stage(user_id, {wiki_id: updated}, set())

    def set_positions(self, user_id: str, positions: dict[str, tuple[float, float]]) -> int:
        """Store layout coordinates for several nodes at once.

        Buffered like every other manifest change, so dragging nodes around
        does not produce one storage write per drag. Unknown wiki_ids are
        skipped rather than creating phantom entries.
        """
        updates: dict[str, dict] = {}
        with self._lock:
            data = self._load_locked(user_id)
            for wiki_id, (x, y) in positions.items():
                entry = data.get(wiki_id)
                if entry is None:
                    continue  # unknown id: skip rather than create a phantom
                updated = dict(entry)
                updated["x"], updated["y"] = float(x), float(y)
                updates[wiki_id] = updated
        if updates:
            self._stage(user_id, updates, set())
        return len(updates)

    def list_entries(self, user_id: str, type_filter: str | None = None) -> list[ManifestEntry]:
        data = self._snapshot(user_id)
        entries = [ManifestEntry.from_dict(v) for v in data.values()]
        if type_filter:
            entries = [e for e in entries if e.type == type_filter]
        return entries

    def get_entry(self, user_id: str, wiki_id: str) -> ManifestEntry | None:
        raw = self._snapshot(user_id).get(wiki_id)
        return ManifestEntry.from_dict(raw) if raw else None

    def compact(self, user_id: str) -> dict:
        """Force a snapshot now. Exposed so operators can fold deltas on a
        schedule rather than waiting for the size trigger."""
        with self._lock:
            self._flush_now_locked()
            self._load_locked(user_id)
            before = len(self._delta_keys.get(user_id, []))
            self._compact_locked(user_id)
            return {"deltas_folded": before, "entries": len(self._cache.get(user_id, {}))}

    def rebuild(self, user_id: str) -> int:
        """Reconstruct the index from the entity files — the recovery path
        that makes buffering safe. Writes a fresh snapshot and drops deltas."""
        from app.graph.store import EntityGraphStore
        data: dict = {}
        for key in self.backend.list_keys(f"{user_id}/wiki/"):
            if not key.endswith(".md"):
                continue
            raw = self.backend.get_bytes(key)
            if raw is None:
                continue
            entity = EntityGraphStore._deserialize(raw.data)
            data[entity.wiki_id] = ManifestEntry(
                wiki_id=entity.wiki_id, type=entity.type, title=entity.title,
                aliases=entity.aliases, path=key, compact=entity.compact,
                status=entity.status, updated_at=entity.metadata.updated_at,
                last_accessed=entity.metadata.last_accessed,
                edges=[{"t": r.target, "c": r.category} for r in entity.relations],
            ).to_dict()
        with self._lock:
            self._cache[user_id] = data
            self._staged[user_id] = {"upsert": {}, "remove": set()}
            self._load_delta_keys_locked(user_id)
            self._compact_locked(user_id)
        return len(data)

    def _load_delta_keys_locked(self, user_id: str) -> None:
        self._delta_keys[user_id] = sorted(
            k for k in self.backend.list_keys(self._delta_prefix(user_id)) if k.endswith(".json")
        )
