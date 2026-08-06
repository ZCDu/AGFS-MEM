"""Async composition root for the memory API and compression worker."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import redis.asyncio as redis_async

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.generations import (
    GenerationAssembler,
    GenerationPlanner,
)
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings, load_settings
from short_term_memory.jobs.compression_worker import (
    CompressionWorker,
    EmptySummaryModel,
)
from short_term_memory.jobs.redis_compression_queue import RedisCompressionQueue
from short_term_memory.jobs.redis_rebuild_completion import RedisRebuildCompletion
from short_term_memory.service.app import create_app
from short_term_memory.service.memory_service import MemoryService
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter


class ApproximateTokenEstimator:
    """Provider-free conservative estimator for routing compression work."""

    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int:
        characters = sum(len(str(message.get("content", ""))) for message in messages)
        return max(1, (characters + 3) // 4)


@dataclass
class ServiceRuntime:
    settings: ShortTermMemorySettings
    redis: Any
    headroom_http: Any
    store: AsyncRedisMemoryStore
    queue: RedisCompressionQueue
    completion: RedisRebuildCompletion
    worker: CompressionWorker
    memory_service: MemoryService
    _owns_redis: bool
    _owns_headroom_http: bool
    _closed: bool = False

    @classmethod
    async def start(
        cls,
        settings: ShortTermMemorySettings,
        *,
        redis: Any | None = None,
        headroom_http: Any | None = None,
        own_injected: bool = False,
        token_estimator: Any | None = None,
        summary_model: Any | None = None,
    ) -> "ServiceRuntime":
        """Construct one pool/client graph, closing owned partial state on failure."""

        if not settings.headroom_service.url:
            raise ValueError("HEADROOM_SERVICE_URL is required for the HTTP runtime")
        redis_client = redis
        http_client = headroom_http
        owns_redis = redis is None or own_injected
        owns_http = headroom_http is None or own_injected
        try:
            if redis_client is None:
                redis_client = redis_async.Redis.from_url(
                    settings.redis_session.url,
                    max_connections=settings.api.redis_pool_size,
                    decode_responses=True,
                )
            if http_client is None:
                http_client = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=200, max_keepalive_connections=100
                    ),
                    timeout=settings.headroom_service.timeout_seconds,
                )

            journals = JournalStore(
                VFSAdapter(Path(settings.home).expanduser())
            )
            store = AsyncRedisMemoryStore(
                redis_client, ttl_seconds=settings.redis_session.ttl_seconds
            )
            queue = RedisCompressionQueue(
                redis_client, capacity=settings.compression_queue.capacity
            )
            scope_factory = OptimizationScopeFactory(
                settings.optimization_scope_secret
            )
            completion = RedisRebuildCompletion(
                redis_client,
                store=store,
                scope_factory=scope_factory,
                ttl_seconds=min(
                    settings.redis_session.ttl_seconds,
                    settings.headroom_service.ccr_ttl_seconds,
                ),
            )
            planner = GenerationPlanner(
                store,
                journals,
                max_segments=settings.headroom_service.max_compression_segments,
            )
            assembler = GenerationAssembler(
                max_segments=settings.headroom_service.max_compression_segments
            )
            headroom = AsyncHeadroomClient(
                settings.headroom_service.url,
                timeout_seconds=settings.headroom_service.timeout_seconds,
                http_client=http_client,
            )
            worker = CompressionWorker(
                queue=queue,
                store=store,
                planner=planner,
                headroom=headroom,
                summary_model=summary_model or EmptySummaryModel(),
                compression_model=settings.headroom_service.compression_model,
                scope_factory=scope_factory,
                ccr_ttl_seconds=settings.headroom_service.ccr_ttl_seconds,
                ccr_refresh_seconds=settings.headroom_service.ccr_refresh_seconds,
                max_segments=settings.headroom_service.max_compression_segments,
                worker_concurrency=settings.compression_queue.worker_concurrency,
                completion_publisher=completion,
            )
            policy = HeadroomPolicy(
                context_window_tokens=settings.redis_session.context_window_tokens,
                trigger_ratio=settings.redis_session.trigger_ratio,
                max_messages=settings.redis_session.max_messages,
                max_session_seconds=settings.redis_session.max_session_seconds,
            )
            memory_service = MemoryService(
                store=store,
                journals=journals,
                assembler=assembler,
                compression_queue=queue,
                policy=policy,
                scope_factory=scope_factory,
                settings=settings,
                token_estimator=token_estimator or ApproximateTokenEstimator(),
                headroom_proxy_url=f"{settings.headroom_service.url.rstrip('/')}/v1",
                rebuild_waiter=completion,
            )
            return cls(
                settings=settings,
                redis=redis_client,
                headroom_http=http_client,
                store=store,
                queue=queue,
                completion=completion,
                worker=worker,
                memory_service=memory_service,
                _owns_redis=owns_redis,
                _owns_headroom_http=owns_http,
            )
        except BaseException:
            partial_closers = []
            if owns_http and http_client is not None:
                partial_closers.append(http_client.aclose())
            if owns_redis and redis_client is not None:
                partial_closers.append(redis_client.aclose())
            if partial_closers:
                await asyncio.gather(*partial_closers, return_exceptions=True)
            raise

    async def readiness(self) -> dict[str, bool]:
        """Return sanitized component booleans; never propagate endpoint details."""

        async def redis_ready() -> bool:
            try:
                return bool(await self.redis.ping())
            except Exception:
                return False

        async def headroom_ready() -> bool:
            try:
                response = await self.headroom_http.get(
                    f"{self.settings.headroom_service.url.rstrip('/')}/health",
                    timeout=min(
                        5.0, self.settings.headroom_service.timeout_seconds
                    ),
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

        redis_ok, headroom_ok = await asyncio.gather(
            redis_ready(), headroom_ready()
        )
        return {"redis": redis_ok, "headroom": headroom_ok}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closers = []
        if self._owns_headroom_http:
            closers.append(self.headroom_http.aclose())
        if self._owns_redis:
            closers.append(self.redis.aclose())
        if not closers:
            return
        results = await asyncio.gather(*closers, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result


class _StartingMemoryService:
    async def write(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("memory runtime has not started")

    async def read(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("memory runtime has not started")


def create_runtime_app(
    settings: ShortTermMemorySettings | None = None,
    *,
    runtime_start: Callable[
        [ShortTermMemorySettings], Awaitable[ServiceRuntime]
    ] = ServiceRuntime.start,
):
    """Uvicorn app factory; every process owns exactly one async runtime."""

    effective_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        runtime = await runtime_start(effective_settings)
        app.state.service_runtime = runtime
        app.state.memory_service = runtime.memory_service
        try:
            yield
        finally:
            await runtime.close()

    return create_app(
        lambda: _StartingMemoryService(),
        settings=effective_settings,
        lifespan=lifespan,
    )
