"""
StorageBackend implementation on top of a mirage Workspace, so this app can
use the same storage layer (mirage-ai) as the original memory-system repo,
instead of raw boto3.

Key facts this was written against (verified against the installed
mirage-ai package, not assumed):

  - Workspace.ops is ASYNC (read/write/stat/unlink/readdir/mkdir are all
    coroutines). Our StorageBackend interface is synchronous (matches
    LocalFSBackend/S3Backend, and how EntityGraphStore/RawFactLog call it).
    MirageBackend bridges this with a dedicated background thread running
    its own persistent event loop — the Workspace and its resources
    (S3Resource, aioboto3 session, etc.) are constructed on that loop once
    and reused for the process lifetime, rather than spinning up a fresh
    event loop (and fresh connections) on every call.

  - ops.write() has NO conditional/compare-and-swap parameter — mirage
    does not support atomic conditional writes the way S3's native
    If-Match does. The original repo's own code (core/entity_graph.py)
    handles this with an in-process asyncio.Lock around its
    read-modify-write sequences, and explicitly documents that this only
    protects a single process, not multiple workers/replicas. We do the
    same thing here: put_bytes(if_match=...) is a best-effort check via
    stat().fingerprint, NOT an atomic guarantee. See the caveat below.

  - ops.stat(path).fingerprint is resource-dependent: for local/disk
    resources it's a modified-timestamp; for S3-backed resources it's
    generally the ETag. Either way, it changes when content changes, which
    is all we need it for here (change detection, not cryptographic
    integrity).

  - ops.readdir(path) returns full paths of direct children (not
    recursive) — matches how the original repo lists entity files.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading

# mirage is a HARD dependency, not an optional extra. Storage for this
# service goes through a mirage Workspace; there is no non-mirage
# production path. These imports are deliberately at module scope so that a
# missing or broken mirage install fails loudly at import time, and so that
# the dependency is visible at the top of the file rather than buried in
# function bodies. (They used to be deferred inside from_s3_config() and
# list_keys(), which worked but made the file read as though it had no
# mirage dependency at all.)
from mirage import DiskResource, MountMode, Workspace
from mirage.cache.file.config import CacheConfig
from mirage.cache.index.config import IndexConfig
from mirage.resource.s3 import S3Config, S3Resource
from mirage.types import FileType

from app.storage.backend import ConflictError, GetResult, StorageBackend

logger = logging.getLogger("memory_backend.mirage")

S3_MOUNT = "/s3"


def _etag_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# Mirage ships a two-layer cache on every Workspace: an INDEX cache for
# listings/metadata and a FILE cache for object bytes.
#
# The file cache is safe for us — measured, reads reflect writes made by
# other processes immediately.
#
# The index cache is NOT safe at its default 600s TTL. Measured behaviour:
# a Workspace sees its own writes in a listing straight away, but does NOT
# see writes made by another process until the TTL expires, under BOTH
# ConsistencyPolicy.LAZY and ConsistencyPolicy.ALWAYS. Only ttl=0 is fresh
# (1s and 5s still returned stale listings).
#
# list_keys() is load-bearing here in ways where staleness is not a
# performance detail but silent data loss:
#   - WikiManifest reads its delta chain by listing _manifest/d/. A missed
#     delta silently discards another worker's writes.
#   - WikiOpsLog.read_day lists segments; a missed segment drops audit records.
#   - WikiManifest.rebuild() lists entity files and REPLACES the index with
#     what it finds. Rebuilding from a stale listing deletes index entries
#     for entities that exist — and rebuild is the recovery path, so it
#     would corrupt exactly when someone is trying to fix things.
#
# Hence ttl=0 by default. Raise MIRAGE_INDEX_TTL_SECONDS only if you run a
# single writer process and want the listing speedup.
DEFAULT_INDEX_TTL = 0.0
DEFAULT_FILE_CACHE_LIMIT = "512MB"

# Whether put_bytes(if_match=...) re-reads the object to verify its ETag
# before overwriting.
#
# What the check actually buys: mirage has no compare-and-swap, so this is a
# read-then-write. Within ONE process it is meaningful, because put_bytes
# holds a per-key asyncio lock across both steps, so concurrent threads in
# the same worker cannot lose updates. Across processes it was never a real
# guarantee — see the caveat in the class docstring.
#
# What it costs: one extra round-trip on every conditional write. On a
# backend where a round-trip is ~600ms that is a third of the cost of every
# write in the system, spent re-reading bytes the caller usually just read.
#
# Leave this True unless round-trip latency dominates AND you run a single
# writer thread. Turning it off trades in-process lost-update protection for
# roughly 33% fewer ops per write.
DEFAULT_VERIFY_CONDITIONAL_WRITES = True


# ---------------------------------------------------------------------------
# Connection reuse
#
# mirage/core/s3/_client.py builds a brand new aioboto3 Session AND a brand
# new client for EVERY operation:
#
#     session = async_session(config)
#     async with session.client(**_client_kwargs(config)) as client:
#         resp = await client.get_object(...)
#
# Nothing is cached or pooled, so every read, write, stat, listing and delete
# pays a full DNS + TCP + TLS handshake and then throws the connection away.
# A TLS handshake is 2-3 round-trips, which against a remote endpoint means a
# flat per-operation floor that is identical for every operation type
# regardless of payload. Measured against a Qiniu endpoint: ~600ms for GET,
# PUT, DELETE and LIST alike, where the underlying RTT is a fraction of that.
#
# We cannot change mirage's call sites, but we can make the client they ask
# for a reused one. This patches async_session so the returned object hands
# back a long-lived client and treats __aexit__ as a no-op, leaving the
# connection open for the next operation.
#
# This is only safe because MirageBackend owns exactly one persistent event
# loop (see _EventLoopThread): aioboto3 clients are bound to the loop that
# created them, so a shared client would be unsafe under the more usual
# pattern of a fresh loop per call. Clients are closed in close().
#
# Set MIRAGE_REUSE_CONNECTIONS=false to disable and get mirage's stock
# behaviour back.
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
# key -> (context_manager, client). The key must bind the client to the
# exact asyncio loop that created it, because aiohttp/aioboto3 clients are
# NOT loop-safe: a client created on loop A will raise "Future attached to
# a different loop" the moment it is reused from loop B. The original
# implementation keyed only by config signature, so a client created on the
# FastAPI main loop (e.g. during startup) would be reused by the backend's
# dedicated loop thread on the next request and crash every read. Keying by
# the loop object + signature keeps the reuse win (the backend owns exactly
# one loop) while making cross-loop reuse impossible.
_pooled: dict[tuple, tuple] = {}   # (loop, signature) -> (context_manager, client)
_patched = False


def _signature(kwargs: dict) -> str:
    """Identify a client configuration. Only the connection-defining parts
    matter; botocore Config objects are not hashable, so pull out the fields
    that actually change how the connection is made."""
    cfg = kwargs.get("config")
    style = ""
    try:
        style = str(getattr(cfg, "s3", "")) + str(getattr(cfg, "proxies", ""))
    except Exception:
        pass
    return "|".join([
        str(kwargs.get("service_name")),
        str(kwargs.get("region_name")),
        str(kwargs.get("endpoint_url")),
        str(kwargs.get("aws_access_key_id")),
        style,
    ])


class _ReusableClientCM:
    """Async context manager that yields a cached client and does NOT close
    it on exit, so the underlying HTTPS connection survives for reuse."""

    def __init__(self, session, kwargs: dict):
        self._session = session
        self._kwargs = kwargs
        self._sig = _signature(kwargs)

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        key = (loop, self._sig)
        with _pool_lock:
            entry = _pooled.get(key)
            if entry is not None:
                return entry[1]
            # No existing client for THIS loop+signature; we create one.
            # The await must happen outside the lock (it is async I/O),
            # so a second caller may reach the same spot concurrently —
            # the second creation below is reconciled so only one survives.
        ctx = self._session.client(**self._kwargs)
        client = await ctx.__aenter__()
        with _pool_lock:
            entry = _pooled.get(key)
            if entry is not None:
                # A concurrent creator won the race for this exact loop.
                # Close the one we just built and hand back the winner so
                # we never leak a socket for a loop that will not use it.
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                return entry[1]
            _pooled[key] = (ctx, client)
            return client

    async def __aexit__(self, *exc_info):
        # Deliberately does not close: that is the entire point.
        return False


class _PooledSession:
    def __init__(self, real):
        self._real = real

    def client(self, **kwargs):
        return _ReusableClientCM(self._real, kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_connection_reuse() -> None:
    global _patched
    with _pool_lock:
        if _patched:
            return
        import aioboto3
        from mirage.core.s3 import _client as mirage_client

        original = mirage_client.async_session

        def pooled_async_session(config):
            return _PooledSession(aioboto3.Session(
                profile_name=getattr(config, "aws_profile", None) or None))

        pooled_async_session._original = original  # type: ignore[attr-defined]
        mirage_client.async_session = pooled_async_session

        # The core op modules imported async_session by name at import time,
        # so rebinding it on _client alone would miss them.
        import importlib
        import pkgutil
        import mirage.core.s3 as core_s3
        for mod in pkgutil.iter_modules(core_s3.__path__):
            try:
                m = importlib.import_module(f"mirage.core.s3.{mod.name}")
            except Exception:
                continue
            if getattr(m, "async_session", None) is original:
                m.async_session = pooled_async_session

        _patched = True
        logger.info("mirage S3 connection reuse enabled")


async def _close_pooled_clients() -> None:
    entries = list(_pooled.items())
    _pooled.clear()
    for _sig, (ctx, _client) in entries:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass


class _EventLoopThread:
    """A background thread running one persistent asyncio event loop.
    Lets synchronous callers run coroutines against long-lived async
    resources (the mirage Workspace) without those resources getting
    rebuilt or detached from their loop on every call."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro, timeout: float | None = 30.0):
        if self._stopped:
            # Submitting to a stopped loop otherwise blocks for the full
            # timeout on a future nothing will ever complete. Write-behind
            # flush timers can fire after close(), so this path is reachable
            # in normal shutdown, not just on misuse.
            coro.close()
            raise RuntimeError("event loop thread is stopped; backend already closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


class MirageBackend(StorageBackend):
    """
    Talks to cloud storage through a mirage Workspace's /s3 mount, instead
    of boto3 directly. Two ways to construct it:

      MirageBackend.from_s3_config(bucket=..., region=..., ...)
          Builds a Workspace with an S3Resource mount — same shape as the
          original repo's mirage_storage._build_workspace(). This is what
          you want for real S3 (or an S3-compatible gateway, e.g. Qiniu
          Kodo, by also passing endpoint_url).

      MirageBackend(workspace=existing_workspace)
          Pass in an already-built mirage Workspace (e.g. if you're
          sharing one Workspace across S3 + Redis mounts the way the
          original app's mirage_workspace.build_workspace() does). The
          object mount is assumed to be S3_MOUNT ("/s3").

    Caveat (read before relying on this under concurrent writers):
    put_bytes(if_match=...) is a BEST-EFFORT check, not an atomic one —
    mirage's ops.write() has no compare-and-swap. There is a real,
    if narrow, race between the stat() check and the write() landing.
    This mirrors the original repo's own documented limitation for its
    in-process locks: safe against races within a single worker process,
    NOT safe across multiple worker processes or replicas writing the
    same key concurrently. If you need real atomicity, use S3Backend
    (storage_backend.S3Backend) instead, which uses S3's native If-Match.
    """

    def __init__(self, workspace, object_mount: str = S3_MOUNT, owns_workspace: bool = False,
                 loop_thread: "_EventLoopThread | None" = None,
                 verify_conditional_writes: bool = DEFAULT_VERIFY_CONDITIONAL_WRITES):
        self._verify_conditional_writes = verify_conditional_writes
        self._workspace = workspace
        self._mount = object_mount.rstrip("/")
        self._owns_workspace = owns_workspace
        self._loop_thread = loop_thread or _EventLoopThread()
        # Per-key lock, held only around a single put_bytes call. Narrows
        # (but per the caveat above, does not eliminate) the stat-then-write
        # race for writers going through the SAME MirageBackend instance.
        self._key_locks: dict[str, asyncio.Lock] = {}

    # ---------- construction ----------

    @classmethod
    def from_s3_config(
        cls,
        *,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        aws_profile: str | None = None,
        path_style: bool = False,
        timeout: int = 30,
        key_prefix: str | None = None,
        index_ttl: float = DEFAULT_INDEX_TTL,
        file_cache_limit: str | int = DEFAULT_FILE_CACHE_LIMIT,
        reuse_connections: bool = True,
        verify_conditional_writes: bool = DEFAULT_VERIFY_CONDITIONAL_WRITES,
    ) -> "MirageBackend":
        if reuse_connections:
            _install_connection_reuse()
        loop_thread = _EventLoopThread()

        async def _build():
            config = S3Config(
                bucket=bucket,
                region=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token,
                aws_profile=aws_profile,
                path_style=path_style,
                timeout=timeout,
                key_prefix=key_prefix,
            )
            return Workspace(
                {S3_MOUNT: S3Resource(config)},
                mode=MountMode.WRITE,
                index=IndexConfig(ttl=index_ttl),
                cache=CacheConfig(limit=file_cache_limit),
            )

        workspace = loop_thread.run(_build())
        return cls(workspace, object_mount=S3_MOUNT, owns_workspace=True, loop_thread=loop_thread,
                   verify_conditional_writes=verify_conditional_writes)

    @classmethod
    def from_disk(cls, root: str, index_ttl: float = DEFAULT_INDEX_TTL,
                  file_cache_limit: str | int = DEFAULT_FILE_CACHE_LIMIT,
                  reuse_connections: bool = False,
                  verify_conditional_writes: bool = DEFAULT_VERIFY_CONDITIONAL_WRITES
                  ) -> "MirageBackend":
        """A mirage Workspace mounting a local directory at /s3.

        This is the offline / no-credentials path. It exists so that local
        development and the test suite exercise the SAME code — mirage
        Workspace ops, this backend's conditional-write emulation, its
        recursive list_keys — as a real S3 deployment, differing only in
        which resource is mounted. Swapping in a different StorageBackend
        for local use would mean the code that actually ships is the code
        that never runs during development.
        """
        # Accepted for call-site symmetry with from_s3_config, but pooling is
        # a no-op here: DiskResource opens no network connections, so there is
        # nothing to reuse.
        del reuse_connections
        loop_thread = _EventLoopThread()

        async def _build():
            return Workspace(
                {S3_MOUNT: DiskResource(root=root)},
                mode=MountMode.WRITE,
                index=IndexConfig(ttl=index_ttl),
                cache=CacheConfig(limit=file_cache_limit),
            )

        workspace = loop_thread.run(_build())
        return cls(workspace, object_mount=S3_MOUNT, owns_workspace=True, loop_thread=loop_thread,
                   verify_conditional_writes=verify_conditional_writes)

    def close(self) -> None:
        """Shut down in dependency order.

        EntityGraphStore attaches write-behind buffers (the manifest and ops
        log) to the backend instance, so those buffers outlive any single
        request and can still hold unwritten state here. They flush by
        calling back into put_bytes, which needs this event loop alive — so
        they must be drained BEFORE the loop stops. Getting this backwards
        deadlocks: the buffer's flush timer fires after the loop is gone and
        blocks on a future that will never complete.
        """
        for attr in ("_wiki_manifest", "_wiki_ops_log", "_embedding_index"):
            buf = getattr(self, attr, None)
            if buf is not None:
                try:
                    buf.close()          # flushes, then stops its timer
                except Exception:
                    pass
        if self._owns_workspace and self._workspace is not None:
            self._loop_thread.run(self._workspace.close())
        # Pooled clients live on this loop, so they must be closed before it
        # stops or their sockets leak.
        try:
            self._loop_thread.run(_close_pooled_clients())
        except Exception:
            pass
        self._loop_thread.stop()

    # ---------- path helper ----------

    def _path(self, key: str) -> str:
        return f"{self._mount}/{key.lstrip('/')}"

    def _lock_for(self, key: str) -> asyncio.Lock:
        # Called from inside the loop thread's own coroutines only, so no
        # cross-thread race creating the dict entry.
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    # ---------- StorageBackend interface ----------

    def get_bytes(self, key: str) -> GetResult | None:
        async def _do():
            path = self._path(key)
            try:
                data = await self._workspace.ops.read(path)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return None
            # Local hash, not a second network round-trip to stat() the
            # object — we already have the bytes in hand, no need to ask
            # the server to tell us something we can compute ourselves.
            # (This used to cost 2 round-trips per get_bytes; now it's 1.)
            return GetResult(data=data, etag=_etag_for(data))

        return self._loop_thread.run(_do())

    def get_many(self, keys: list[str]) -> dict[str, GetResult | None]:
        """Concurrent multi-get on the shared event loop.

        Mirage's ops are natively async, and this backend already owns a
        persistent loop, so N independent reads cost roughly ONE round-trip
        of wall time instead of N. That is the single largest latency win
        available against real S3, where each GET is tens of milliseconds of
        pure network wait.

        Reads only, and only for independent keys — this does no ordering or
        conflict handling, so it must never be used for read-modify-write.
        """
        if not keys:
            return {}

        async def _one(key: str):
            path = self._path(key)
            try:
                data = await self._workspace.ops.read(path)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return key, None
            return key, GetResult(data=data, etag=_etag_for(data))

        async def _do():
            # return_exceptions keeps one bad key from failing the batch;
            # a caller loop would have skipped it individually anyway.
            results = await asyncio.gather(*(_one(k) for k in keys), return_exceptions=True)
            out: dict[str, GetResult | None] = {}
            for item in results:
                if isinstance(item, BaseException):
                    continue
                key, value = item
                out[key] = value
            for k in keys:
                out.setdefault(k, None)
            return out

        return self._loop_thread.run(_do())

    def put_bytes(self, key: str, data: bytes, if_match: str | None = None) -> str:
        async def _do():
            path = self._path(key)
            async with self._lock_for(key):
                if if_match is not None and not self._verify_conditional_writes:
                    # Skipping the verification read. The per-key lock above
                    # still serialises writers in this process, but a lost
                    # update is now possible if two of them raced between
                    # their reads. Opt-in only.
                    pass
                elif if_match is not None:
                    current_etag = await self._current_fingerprint(path)
                    if if_match == "":
                        if current_etag is not None:
                            raise ConflictError(f"{key} already exists")
                    elif current_etag != if_match:
                        raise ConflictError(
                            f"ETag mismatch for {key}: expected {if_match!r}, found {current_etag!r}"
                        )
                await self._workspace.ops.write(path, data)
                # Local hash of what we just wrote, not a third round-trip
                # to stat() it back. (This used to cost 3 round-trips per
                # put_bytes with if_match; now it's 2 — one real read for
                # the conflict check, one write. That read is the minimum
                # possible for "verify nothing changed before I overwrite
                # it" without mirage exposing a native conditional-write
                # primitive — see the module docstring's caveat on this.)
                return _etag_for(data)

        return self._loop_thread.run(_do())

    def delete(self, key: str) -> None:
        async def _do():
            try:
                await self._workspace.ops.unlink(self._path(key))
            except (FileNotFoundError, NotADirectoryError):
                pass

        self._loop_thread.run(_do())

    def list_keys(self, prefix: str) -> list[str]:
        """
        RECURSIVE prefix listing, to match the StorageBackend contract.

        This is the one place mirage's semantics differ sharply from the
        other two backends and it is easy to get wrong. LocalFSBackend uses
        os.walk() and S3Backend uses list_objects_v2(Prefix=...) — both
        return every key under the prefix at any depth. mirage's
        ops.readdir() is a POSIX-style directory listing: immediate
        children only, directories included as entries.

        Returning readdir()'s output directly would mean list_keys("u/wiki/")
        yields ["u/wiki/person", "u/wiki/_manifest"] — directory names —
        instead of the nested "u/wiki/person/alice-chen.okf.md" that callers
        expect. Every caller that scans for entity files (manifest.rebuild,
        reconcile, stats) would silently see an empty graph. So we walk.

        mirage caches directory listings and metadata in its index cache, so
        the per-entry stat() below is served locally after the first walk
        rather than costing a round-trip each.
        """
        async def _do():
            ops = self._workspace.ops

            async def classify(path: str):
                """Is this a file or a directory? stat() is a round-trip, so
                these are issued concurrently per level rather than one at a
                time — see the walk() note below."""
                try:
                    st = await ops.stat(path)
                    return path, (st.type == FileType.DIRECTORY)
                except (FileNotFoundError, NotADirectoryError):
                    return path, None

            async def walk(dir_path: str) -> list[str]:
                try:
                    children = await ops.readdir(dir_path)
                except (FileNotFoundError, NotADirectoryError):
                    # Absent or not-a-directory is an empty listing, not an
                    # error — LocalFSBackend returns [] for a missing prefix
                    # and callers rely on that (e.g. reading the delta chain
                    # of a manifest that has never been written).
                    return []
                if not children:
                    return []

                # The original version awaited stat() inside a for-loop, so
                # listing N objects cost N SEQUENTIAL round-trips. With the
                # index cache TTL at 0 (required for listing correctness —
                # see DEFAULT_INDEX_TTL) none of those are served locally, so
                # against real S3 a single listing of 25 objects was ~30
                # network waits, several seconds at typical RTT. Issuing them
                # concurrently makes round-trips scale with directory DEPTH
                # instead of object COUNT.
                classified = await asyncio.gather(*(classify(c) for c in children))

                files = [p for p, is_dir in classified if is_dir is False]
                dirs = [p for p, is_dir in classified if is_dir is True]

                if dirs:
                    for sub in await asyncio.gather(*(walk(d) for d in dirs)):
                        files.extend(sub)
                return files

            target = self._path(prefix.rstrip("/"))
            paths = await walk(target)
            if not paths and not prefix.endswith("/"):
                # The prefix may name a partial filename rather than a
                # directory (LocalFSBackend supports this via dirname +
                # startswith). Fall back to the parent and filter.
                parent = target.rsplit("/", 1)[0]
                paths = [p for p in await walk(parent) if p.startswith(target)]

            out = []
            for p in paths:
                rel = p[len(self._mount) + 1:] if p.startswith(self._mount + "/") else p.lstrip("/")
                out.append(rel)
            return sorted(out)

        return self._loop_thread.run(_do())

    # ---------- fingerprint helper ----------

    async def _current_fingerprint(self, path: str) -> str | None:
        """
        Reads the current object and returns its local hash — must use the
        exact same hashing as get_bytes()/put_bytes() above, since a caller's
        if_match value came from an earlier get_bytes() call. This is a real
        read, not a stat(), because we need the actual content to hash
        consistently — mirage doesn't expose a native conditional-write
        primitive we could use instead (see this module's docstring).
        """
        try:
            data = await self._workspace.ops.read(path)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        return _etag_for(data)
