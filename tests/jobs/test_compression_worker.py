import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.generations import GenerationPlanner
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.compression_worker import CompressionWorker, EmptySummaryModel
from short_term_memory.jobs.redis_compression_queue import CompressionJob, RedisCompressionQueue
from short_term_memory.models import (
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
)
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import envelope, memory_event
from tests.jobs.test_redis_compression_queue import QueueRedis
from tests.storage.fake_redis import AsyncFakeRedis


class MarkerTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(
            200,
            json={
                "messages": [{"role": "system", "content": "marker"}],
                "tokens_before": 100,
                "tokens_after": 25,
                "tokens_saved": 75,
                "compression_ratio": 4.0,
                "transforms_applied": ["test"],
            },
            request=request,
        )


class FailingHeadroom:
    async def compress(self, *args, **kwargs):
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.FAILED,
            messages=(),
            fallback_used=False,
            failure_reason=HeadroomFailureReason.SERVICE_UNAVAILABLE,
        )


class BlockingHeadroom:
    def __init__(self):
        self.started = asyncio.Event()

    async def compress(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class DelayedSuccessfulHeadroom:
    async def compress(self, *args, **kwargs):
        return HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=({"role": "system", "content": "marker"},),
            fallback_used=False,
            tokens_before=100,
            tokens_after=25,
        )


async def seed(store, journals, count=10):
    for sequence in range(1, count + 1):
        content = f"ORIGINAL-{sequence}"
        reservation = await store.reserve_event(
            "u", "s", f"event-{sequence}", sha256(content.encode()).hexdigest()
        )
        event = memory_event(sequence=reservation.sequence, event_id=f"event-{sequence}", content=content)
        journals.append_event("u", "s", event)
        await store.commit_event("u", "s", event)


@pytest_asyncio.fixture
async def worker(tmp_path):
    store = AsyncRedisMemoryStore(AsyncFakeRedis())
    journals = JournalStore(VFSAdapter(tmp_path))
    await seed(store, journals)
    transport = MarkerTransport()
    headroom = AsyncHeadroomClient("http://headroom:8787", timeout_seconds=5, transport=transport)
    worker = CompressionWorker(
        queue=RedisCompressionQueue(QueueRedis()),
        store=store,
        planner=GenerationPlanner(store, journals, max_segments=8),
        headroom=headroom,
        summary_model=EmptySummaryModel(),
        compression_model="deepseek-v4-flash",
        scope_factory=OptimizationScopeFactory("secret"),
        ccr_ttl_seconds=43_200,
        ccr_refresh_seconds=3_600,
        max_segments=8,
    )
    yield worker, store, transport
    await headroom.aclose()


def compression_job(*, through_sequence=10, expected_version=0):
    return CompressionJob(
        job_id=f"job-{through_sequence}-{expected_version}", user_id="u", session_id="s",
        expected_version=expected_version, requested_through_sequence=through_sequence, attempt=0,
    )


@pytest.mark.asyncio
async def test_worker_stores_generation_only_after_headroom_success(worker):
    worker, store, transport = worker
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()
    persisted = await store.read_envelope("u", "s")

    assert result.state == "acked"
    assert persisted is not None
    assert persisted.compressed_through_sequence == 10
    assert persisted.compression_generations[0].messages[0].content == "marker"
    assert b"ORIGINAL-1" in transport.requests[0].content
    assert b"marker" not in transport.requests[0].content


@pytest.mark.asyncio
async def test_stale_worker_is_acked_without_overwrite(worker):
    worker, store, _ = worker
    assert await store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    await worker.queue.enqueue(compression_job(expected_version=0))

    result = await worker.run_once()

    assert result.state == "stale"
    assert (await store.read_envelope("u", "s")).version == 1


@pytest.mark.asyncio
async def test_worker_reports_lost_when_successful_cas_cannot_ack(worker):
    worker, store, _ = worker
    worker.queue.ack = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "lost"
    assert (await store.read_envelope("u", "s")) is not None


@pytest.mark.asyncio
async def test_worker_reports_lost_when_stale_ack_loses_ownership(worker):
    worker, store, _ = worker
    assert await store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    worker.queue.ack = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job(expected_version=0))

    result = await worker.run_once()

    assert result.state == "lost"


@pytest.mark.asyncio
async def test_headroom_failure_retries_without_advancing_envelope(worker):
    worker, store, _ = worker
    worker.headroom = FailingHeadroom()
    await worker.queue.enqueue(compression_job())

    result = await worker.run_once()

    assert result.state == "retry"
    assert await store.read_envelope("u", "s") is None
    assert worker.queue.client.zsets[worker.queue.RETRY_KEY]


@pytest.mark.asyncio
async def test_headroom_failure_uses_fresh_clock_for_retry_deadline(worker):
    worker, _, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.headroom = FailingHeadroom()
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "retry"
    due = worker.queue.client.zsets[worker.queue.RETRY_KEY]["job-10-0"]
    assert due == int(t10.timestamp() * 1_000) + 1_000


@pytest.mark.asyncio
async def test_deferred_session_lease_uses_fresh_clock_for_retry_deadline(worker):
    worker, _, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.store.acquire_compression_lease = AsyncMock(return_value=False)
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "deferred"
    due = worker.queue.client.zsets[worker.queue.RETRY_KEY]["job-10-0"]
    assert due == int(t10.timestamp() * 1_000) + 1_000


@pytest.mark.asyncio
async def test_generation_timestamps_use_the_headroom_completion_clock(worker):
    worker, store, _ = worker
    t0 = datetime(2026, 8, 6, tzinfo=timezone.utc)
    t10 = datetime(2026, 8, 6, 0, 0, 10, tzinfo=timezone.utc)
    clock_values = iter((t0, t10))
    worker.clock = lambda: next(clock_values)
    worker.headroom = DelayedSuccessfulHeadroom()
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "acked"
    generation = (await store.read_envelope("u", "s")).compression_generations[0]
    assert generation.created_at == t10.isoformat()
    assert generation.ccr_expires_at == "2026-08-06T12:00:10+00:00"


@pytest.mark.asyncio
async def test_worker_reports_lost_when_headroom_retry_loses_ownership(worker):
    worker, _, _ = worker
    worker.headroom = FailingHeadroom()
    worker.queue.retry = AsyncMock(return_value="lost")
    await worker.queue.enqueue(compression_job())

    assert (await worker.run_once()).state == "lost"


@pytest.mark.asyncio
async def test_cancelled_worker_job_is_reclaimed_after_its_queue_lease_expires(worker):
    worker, _, _ = worker
    blocking = BlockingHeadroom()
    worker.headroom = blocking
    fixed_now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    worker.clock = lambda: fixed_now
    await worker.queue.enqueue(compression_job())
    task = asyncio.create_task(worker.run_once())
    await blocking.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reclaimed = await worker.queue.lease(
        "replacement", now_unix_ms=int(fixed_now.timestamp() * 1_000) + 300_001
    )
    assert reclaimed is not None and reclaimed.job.job_id == "job-10-0"
