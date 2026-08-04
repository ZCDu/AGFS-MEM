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
    _loaded_env_path = load_dotenv()  # searches CWD and parent dirs for a ".env" file
except ImportError:
    _loaded_env_path = False

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api import (routes_auth, routes_chat, routes_entities, routes_extract,
                     routes_files, routes_health, routes_rawlog, routes_sessions,
                     routes_verify)
from app.config import get_settings
from app.storage.backend import ConflictError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memory_backend")

if _loaded_env_path:
    logger.info("Loaded environment variables from .env")
else:
    logger.info("No .env file loaded (not found, or python-dotenv not installed) — "
                 "using whatever is already in the environment")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
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
        for attr in ("_wiki_manifest", "_wiki_ops_log"):
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
    app.include_router(routes_entities.router)
    app.include_router(routes_rawlog.router)
    app.include_router(routes_verify.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_sessions.router)
    app.include_router(routes_files.router)
    app.include_router(routes_extract.router)

    @app.get("/static/auth.js", include_in_schema=False)
    def auth_js():
        """Shared sign-in widget for both UIs. One implementation cannot drift
        from itself; two copies did."""
        return FileResponse(Path(__file__).parent / "static" / "auth.js",
                            media_type="application/javascript")

    @app.get("/chat", include_in_schema=False)
    def chat_gui():
        """Chat UI. Same-origin for the same reason as /gui: no CORS middleware."""
        return FileResponse(Path(__file__).parent / "static" / "chat.html",
                            media_type="text/html")

    @app.get("/gui", include_in_schema=False)
    def gui():
        """Serve the graph editor from the app's own origin.

        Same-origin on purpose: there is no CORS middleware, so a page opened
        from the filesystem could not call the API at all. Serving it here
        removes the question rather than loosening the API's origin policy.
        """
        return FileResponse(Path(__file__).parent / "static" / "gui.html",
                            media_type="text/html")

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
