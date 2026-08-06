"""Durable original-only Headroom compression worker."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid
from typing import Callable

import anyio

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.generations import CompressionCandidate, GenerationPlanner
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.jobs.redis_compression_queue import (
    CompressionJobLease,
    RedisCompressionQueue,
)
from short_term_memory.models import (
    CompressionGeneration,
    HeadroomCompressionStatus,
    MemorySummaryEnvelope,
    SessionSummaryPayload,
)
from short_term_memory.ports import SummaryModel


class EmptySummaryModel:
    """Standalone worker default that deliberately has no model-provider dependency."""

    def summarize(self, messages: tuple[dict[str, object], ...]) -> SessionSummaryPayload:
        del messages
        return SessionSummaryPayload(
            current_goal=[], preferences=[], confirmed_facts=[], pending_items=[],
            attachment_references=[],
        )


@dataclass(frozen=True)
class CompressionWorkerResult:
    state: str
    job_id: str | None = None


class CompressionWorker:
    def __init__(
        self,
        *,
        queue: RedisCompressionQueue,
        store: object,
        planner: GenerationPlanner,
        headroom: AsyncHeadroomClient,
        summary_model: SummaryModel,
        compression_model: str,
        scope_factory: OptimizationScopeFactory,
        ccr_ttl_seconds: int,
        ccr_refresh_seconds: int,
        max_segments: int,
        worker_concurrency: int = 1,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if min(ccr_ttl_seconds, ccr_refresh_seconds, max_segments, worker_concurrency) < 1:
            raise ValueError("worker limits must be positive")
        self.queue = queue
        self.store = store
        self.planner = planner
        self.headroom = headroom
        self.summary_model = summary_model
        self.compression_model = compression_model
        self.scope_factory = scope_factory
        self.ccr_ttl_seconds = ccr_ttl_seconds
        self.ccr_refresh_seconds = ccr_refresh_seconds
        self.max_segments = max_segments
        self.worker_concurrency = worker_concurrency
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> CompressionWorkerResult:
        now = self._now()
        lease = await self.queue.lease(uuid.uuid4().hex, now_unix_ms=self._unix_ms(now))
        if lease is None:
            return CompressionWorkerResult("idle")
        session_token = uuid.uuid4().hex
        acquired = await self.store.acquire_compression_lease(
            lease.job.user_id, lease.job.session_id, session_token
        )
        if not acquired:
            return await self._retry(lease, "deferred")
        try:
            return await self._execute(lease, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._retry(lease, "retry")
        finally:
            await self.store.release_compression_lease(
                lease.job.user_id, lease.job.session_id, session_token
            )

    async def run_forever(self, *, poll_seconds: float = 0.1) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")

        async def loop() -> None:
            while True:
                result = await self.run_once()
                if result.state == "idle":
                    await asyncio.sleep(poll_seconds)

        await asyncio.gather(*(loop() for _ in range(self.worker_concurrency)))

    async def _execute(
        self, lease: CompressionJobLease, now: datetime
    ) -> CompressionWorkerResult:
        job = lease.job
        envelope = await self.store.read_envelope(job.user_id, job.session_id)
        current_version = envelope.version if envelope is not None else 0
        if current_version != job.expected_version:
            return await self._ack(lease, "stale")

        candidate = await self._candidate(job, envelope, now)
        if candidate is None or candidate.expected_version != job.expected_version:
            return await self._ack(lease, "stale")
        if not candidate.originals:
            return await self._ack(lease, "acked")

        # Headroom input is constructed exclusively from the selected journal
        # originals.  No summary envelope or prior generation is ever included.
        messages = tuple(
            {"role": event.role.value, "content": event.content}
            for event in candidate.originals
        )
        compressed = await self.headroom.compress(
            messages,
            model=self.compression_model,
            correlation_id=job.job_id,
            scope_headers=self.scope_factory.for_session(
                job.user_id, job.session_id
            ).as_headroom_headers(),
        )
        if compressed.status is not HeadroomCompressionStatus.SUCCESS:
            return await self._retry(lease, "retry")

        completed_at = self._now()
        summary = await anyio.to_thread.run_sync(
            self.summary_model.summarize, compressed.messages
        )
        if not isinstance(summary, SessionSummaryPayload):
            summary = SessionSummaryPayload.model_validate(summary)
        next_envelope = self._next_envelope(
            envelope, candidate, compressed, summary, completed_at
        )
        written = await self.store.compare_and_set_envelope(
            job.user_id, job.session_id, job.expected_version, next_envelope
        )
        return await self._ack(lease, "acked" if written else "stale")

    async def _candidate(
        self, job, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> CompressionCandidate | None:
        if job.rebuild:
            return await self.planner.plan_rebuild(
                job.user_id, job.session_id, job.requested_through_sequence
            )
        rebuild_through = max(
            job.requested_through_sequence,
            envelope.compressed_through_sequence if envelope is not None else 0,
        )
        if envelope is not None and self._needs_rebuild(envelope, now):
            return await self.planner.plan_rebuild(
                job.user_id, job.session_id, rebuild_through
            )
        candidate = await self.planner.plan_incremental(job.user_id, job.session_id)
        if candidate is None:
            return None
        originals = tuple(
            event
            for event in candidate.originals
            if event.sequence <= job.requested_through_sequence
        )
        if not originals:
            return None
        return CompressionCandidate(
            user_id=candidate.user_id,
            session_id=candidate.session_id,
            expected_version=candidate.expected_version,
            from_sequence=originals[0].sequence,
            through_sequence=originals[-1].sequence,
            originals=originals,
            rebuild=False,
        )

    def _next_envelope(
        self,
        current: MemorySummaryEnvelope | None,
        candidate: CompressionCandidate,
        compressed,
        summary: SessionSummaryPayload,
        now: datetime,
    ) -> MemorySummaryEnvelope:
        previous = current.compression_generations if current is not None else ()
        generation = CompressionGeneration(
            generation=max((item.generation for item in previous), default=0) + 1,
            from_sequence=candidate.from_sequence,
            through_sequence=candidate.through_sequence,
            messages=compressed.messages,
            tokens_before=compressed.tokens_before or 0,
            tokens_after=compressed.tokens_after or 0,
            created_at=now.isoformat(),
            ccr_expires_at=(now + timedelta(seconds=self.ccr_ttl_seconds)).isoformat(),
        )
        generations = (generation,) if candidate.rebuild else (*previous, generation)
        return MemorySummaryEnvelope(
            version=candidate.expected_version + 1,
            compressed_through_sequence=candidate.through_sequence,
            compression_generations=generations,
            updated_at=now.isoformat(),
            **summary.model_dump(),
        )

    def _needs_rebuild(self, envelope: MemorySummaryEnvelope, now: datetime) -> bool:
        if len(envelope.compression_generations) >= self.max_segments:
            return True
        refresh_at = now + timedelta(seconds=self.ccr_refresh_seconds)
        return any(
            self._aware_datetime(generation.ccr_expires_at) <= refresh_at
            for generation in envelope.compression_generations
        )

    def _now(self) -> datetime:
        return self._aware_datetime(self.clock())

    @staticmethod
    def _aware_datetime(value: datetime | str) -> datetime:
        result = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("worker clock and CCR timestamps must be timezone-aware")
        return result

    @staticmethod
    def _unix_ms(value: datetime) -> int:
        return int(value.timestamp() * 1_000)

    async def _retry(
        self, lease: CompressionJobLease, state: str
    ) -> CompressionWorkerResult:
        retry_now = self._now()
        result = await self.queue.retry(lease, now_unix_ms=self._unix_ms(retry_now))
        if result == "dead":
            return CompressionWorkerResult("dead", lease.job.job_id)
        if result == "lost":
            return CompressionWorkerResult("lost", lease.job.job_id)
        return CompressionWorkerResult(state, lease.job.job_id)

    async def _ack(self, lease: CompressionJobLease, state: str) -> CompressionWorkerResult:
        return CompressionWorkerResult(
            state if await self.queue.ack(lease) else "lost", lease.job.job_id
        )


class InProcessRebuildWaiter:
    """Explicit worker-service boundary for a bounded cold-rebuild wait."""

    def __init__(self, worker: CompressionWorker) -> None:
        self.worker = worker
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def wait_for(self, job, timeout_seconds: float) -> MemorySummaryEnvelope | None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        lock = self._locks.setdefault((job.user_id, job.session_id), asyncio.Lock())
        async with lock:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    envelope = await self.worker.store.read_envelope(
                        job.user_id, job.session_id
                    )
                    if self._matches(job, envelope):
                        return envelope
                    result = await self.worker.run_once()
                    if result.state == "acked":
                        envelope = await self.worker.store.read_envelope(
                            job.user_id, job.session_id
                        )
                        if self._matches(job, envelope):
                            return envelope
                    if result.job_id == job.job_id:
                        envelope = await self.worker.store.read_envelope(
                            job.user_id, job.session_id
                        )
                        if self._matches(job, envelope):
                            return envelope
                        return None
                    if result.state == "idle":
                        await asyncio.sleep(0.01)

    @staticmethod
    def _matches(job, envelope: MemorySummaryEnvelope | None) -> bool:
        return bool(
            envelope is not None
            and envelope.version > job.expected_version
            and envelope.compressed_through_sequence >= job.requested_through_sequence
        )
