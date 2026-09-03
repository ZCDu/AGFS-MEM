from __future__ import annotations

import logging
import time

# Load .env (if present) into os.environ BEFORE anything else runs — this
# must happen before app.deps/app.config read any env vars, since
# get_settings()/get_storage_backend() are cached on first access. Falls
# back gracefully (env vars set the old way still work) if python-dotenv
# isn't installed, so this isn't a hard new dependency.
try:
    from dotenv import load_dotenv
    # Default override=False: .env fills in env vars that are not already
    # set, but never shadows explicit env (incl. test harness monkeypatch).
    # For prod the stale-key trap is avoided by launching the server from a
    # shell without a leftover DEEPSEEK_API_KEY, not by force-overriding.
    _loaded_env_path = load_dotenv()  # searches CWD and parent dirs for a ".env" file
except ImportError:
    _loaded_env_path = False

# Verify TLS against the OS's own trust store instead of OpenSSL's bundled
# logic. Some real, legitimately-trusted CA certs (DeepSeek's chain among
# them, observed directly) mark their Basic Constraints extension in a way
# OpenSSL 3.x now rejects outright ("Basic Constraints of CA cert not marked
# critical") even though the OS itself -- and therefore curl, browsers, and
# every non-Python client -- accepts the exact same chain without
# complaint. That is a validation-strictness mismatch, not evidence of a
# untrusted certificate, so the fix is to defer to the trust decision the OS
# already makes correctly (truststore.inject_into_ssl() patches ssl.SSLContext
# to do exactly that), not to weaken verification. Must run before any
# module creates an SSL context; this is the first thing the app does.
# Soft dependency, same reasoning as python-dotenv above: absence just means
# outbound HTTPS (the LLM client) keeps using OpenSSL's stricter default.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import (routes_auth, routes_chat, routes_entities, routes_extract,
                     routes_files, routes_health, routes_rawlog, routes_sessions,
                     routes_verify, routes_wikis, routes_autocapture, routes_wiki_entities,
                     routes_intents, routes_notes, routes_views, routes_shares)
from app.config import get_settings
from app.storage.backend import ConflictError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memory_backend")

# The background auto-capture timer. Created lazily by the lifespan so tests
# and non-server entry points never start a thread they did not ask for.
_auto_capture_timer = None

if _loaded_env_path:
    logger.info("Loaded environment variables from .env")
else:
    logger.info("No .env file loaded (not found, or python-dotenv not installed) — "
                 "using whatever is already in the environment")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Start the background auto-capture timer (no-op unless AUTO_CAPTURE_ENABLED).
    global _auto_capture_timer
    try:
        from app.autocapture.timer import AutoCaptureTimer
        _auto_capture_timer = AutoCaptureTimer(get_settings())
        _auto_capture_timer.start()
    except Exception:
        logger.exception("could not initialise auto-capture timer")
    yield
    if _auto_capture_timer is not None:
        _auto_capture_timer.stop()
        _auto_capture_timer = None
    # Close the BACKEND, not just the buffers. MirageBackend.close() drains
    # the write-behind buffers and then stops its event loop, in that order.
    #
    # This must happen here, during graceful shutdown, and cannot be left to
    # the atexit hook in app/storage/writebehind.py. CPython shuts thread
    # pools down before it runs atexit handlers, so by then mirage's aiofiles
    # executor is gone and any flush raises "cannot schedule new futures
    # after shutdown" — buffered manifest deltas and audit records would be
    # lost. atexit remains as a backstop for non-server entry points; this is
    # the path that actually works.
    from app.deps import get_storage_backend
    backend = get_storage_backend()
    close = getattr(backend, "close", None)
    if callable(close):
        close()
    else:
        for attr in ("_wiki_manifest", "_wiki_ops_log", "_embedding_index"):
            buf = getattr(backend, attr, None)
            if buf is not None:
                buf.close()


def create_app() -> FastAPI:
    # Fail closed: a misconfiguration must be a startup crash with
    # instructions, not a silently unauthenticated API.
    from app.auth import validate_at_startup
    validate_at_startup(get_settings())

    app = FastAPI(
        lifespan=_lifespan,
        title="Memory Graph Backend",
        description=(
            "S3-backed entity graph (markdown + YAML front-matter) and "
            "sharded raw fact log (JSONL). See README for design notes."
        ),
        version="0.1.0",
    )

    from app.graph.store import MalformedEntityError, SlugConflictError

    @app.exception_handler(SlugConflictError)
    async def _slug_conflict(request: Request, exc: SlugConflictError):
        # 409, not 422: the request is well-formed, it just collides with
        # existing state. The body carries both titles and a ready-to-use
        # alternative so the caller can decide without a second lookup.
        logger.info("slug conflict on %s: %s", request.url.path, exc)
        return JSONResponse(status_code=409, content={
            "detail": str(exc),
            "wiki_id": exc.wiki_id,
            "existing_title": exc.existing_title,
            "incoming_title": exc.incoming_title,
            "suggested_wiki_id": exc.suggestion,
        })


    @app.exception_handler(MalformedEntityError)
    async def _malformed_entity(request: Request, exc: MalformedEntityError):
        # An entity file exists but cannot be parsed. That is neither "not
        # found" nor an unexpected server fault, and reporting it as a bare
        # 500 hides which file is broken. Name the cause so the operator can
        # fix or delete it.
        logger.warning("Corrupt entity file on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc),
                     "hint": "Entity file is corrupt. Delete it, restore it, or run "
                             "POST /v1/users/{user_id}/wiki/_rebuild_manifest after removing it."},
        )

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        """Report how long the server itself spent on the request.

        Without this, a client can only measure wall-clock round-trip, which
        bundles together three very different things: actual server work,
        network/loopback latency, and client-side overhead (on Windows,
        Invoke-RestMethod's own cost is far from negligible). When a CRUD
        call looks slow, that distinction is the whole diagnosis — object
        storage latency and client overhead call for opposite fixes.

        perf_counter is monotonic, so it is unaffected by clock adjustments.
        """
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - start) * 1000:.3f}"
        return response

    app.include_router(routes_health.router)
    app.include_router(routes_auth.router)
    # routes_views must come BEFORE routes_entities: several view/diary paths
    # under /v1/users/{u}/wiki/... are 2-segment GETs that would otherwise lose
    # to the entity router's /wiki/{type}/{title} match (FastAPI resolves in
    # registration order). Declaring the literal view routes first wins.
    app.include_router(routes_views.router)
    app.include_router(routes_entities.router)
    app.include_router(routes_rawlog.router)
    app.include_router(routes_verify.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_sessions.router)
    app.include_router(routes_files.router)
    app.include_router(routes_wikis.router)
    app.include_router(routes_wiki_entities.router)
    app.include_router(routes_extract.router)
    app.include_router(routes_autocapture.router)
    app.include_router(routes_intents.router)
    app.include_router(routes_notes.router)
    app.include_router(routes_shares.owner_router)
    app.include_router(routes_shares.guest_router)

    def _no_cache(res):
        """These pages embed a lot of logic and the files are edited in place.
        A long-lived Cache-Control would keep servers/browsers serving a stale
        copy long after the code changed, which reads as a broken UI. Force
        revalidation so a refresh always sees the current widgets."""
        res.headers["Cache-Control"] = "no-cache"

    @app.get("/static/auth.js", include_in_schema=False)
    def auth_js():
        """Shared sign-in widget for both UIs. One implementation cannot drift
        from itself; two copies did."""
        res = FileResponse(Path(__file__).parent / "static" / "auth.js",
                           media_type="application/javascript")
        _no_cache(res)
        return res

    @app.get("/chat", include_in_schema=False)
    def chat_gui():
        """Chat UI. Same-origin for the same reason as /gui: no CORS middleware."""
        res = FileResponse(Path(__file__).parent / "static" / "chat.html",
                           media_type="text/html")
        _no_cache(res)
        return res

    @app.get("/gui", include_in_schema=False)
    def gui():
        """Serve the graph editor from the app's own origin.

        Same-origin on purpose: there is no CORS middleware, so a page opened
        from the filesystem could not call the API at all. Serving it here
        removes the question rather than loosening the API's origin policy.
        """
        res = FileResponse(Path(__file__).parent / "static" / "gui.html",
                           media_type="text/html")
        _no_cache(res)
        return res

    @app.get("/shared/{share_id}", include_in_schema=False)
    def shared_gui(share_id: str):
        """Guest-facing chat for a share link. Deliberately a separate, small
        page rather than a mode of /chat's much larger UI (session list, wiki
        picker, file uploads, sign-in) -- none of that applies to a guest who
        has only a link and no account. share_id itself is read client-side
        from the URL; the page's own API calls are what actually resolve it
        server-side (see routes_shares.py)."""
        res = FileResponse(Path(__file__).parent / "static" / "shared.html",
                           media_type="text/html")
        _no_cache(res)
        return res

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        # Surfaces as 409 rather than a raw 500 — this fires when a write loses
        # a concurrency race after exhausting its retries (see MAX_WRITE_RETRIES
        # in app/graph/store.py and app/rawlog/log.py). A client seeing this
        # should retry the whole operation.
        logger.warning("Storage write conflict (exhausted retries): %s", exc)
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()

