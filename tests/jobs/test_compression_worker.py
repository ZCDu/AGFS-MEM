from hashlib import sha256

import httpx
import pytest
import pytest_asyncio

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.generations import GenerationPlanner
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.compression_worker import CompressionWorker, EmptySummaryModel
from short_term_memory.jobs.redis_compression_queue import CompressionJob, RedisCompressionQueue
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
    worker = CompressionWorker(
        queue=RedisCompressionQueue(QueueRedis()),
        store=store,
        planner=GenerationPlanner(store, journals, max_segments=8),
        headroom=AsyncHeadroomClient("http://headroom:8787", timeout_seconds=5, transport=transport),
        summary_model=EmptySummaryModel(),
        compression_model="deepseek-v4-flash",
        scope_factory=OptimizationScopeFactory("secret"),
        ccr_ttl_seconds=43_200,
        ccr_refresh_seconds=3_600,
        max_segments=8,
    )
    yield worker, store, transport
    await worker.headroom.aclose()


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
