"""Write-ahead memory use cases with non-blocking compression intent."""

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import time
from typing import Any, Callable
from uuid import uuid5, NAMESPACE_URL

import anyio

from short_term_memory.compression.generations import GenerationAssembler
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.jobs.redis_compression_queue import CompressionJob
from short_term_memory.models import MemoryEvent, MemorySummaryEnvelope
from short_term_memory.service.schemas import (
    EffectiveMemoryConfig,
    HeadroomProxyContext,
    MemoryReadRequest,
    MemoryReadResponse,
    MemoryReadState,
    MemoryWriteRequest,
    MemoryWriteResponse,
    ReadTiming,
    WriteTiming,
)
from short_term_memory.storage.async_redis_memory_store import EventConflictError
from short_term_memory.storage.journal_store import JournalConflictError, JournalStore


class RetryableWriteError(RuntimeError):
    """A journaled event could not yet be committed to Redis."""

    def __init__(self, event_id: str, committed_event_ids: tuple[str, ...]) -> None:
        super().__init__(f"Redis commit failed for event_id {event_id!r}; retry safely")
        self.event_id = event_id
        self.committed_event_ids = committed_event_ids


class MemoryService:
    """Online use cases; it never invokes Headroom or a model provider."""

    def __init__(
        self,
        *,
        store: Any,
        journals: JournalStore,
        assembler: GenerationAssembler,
        compression_queue: Any,
        policy: HeadroomPolicy,
        scope_factory: OptimizationScopeFactory,
        settings: ShortTermMemorySettings,
        token_estimator: Any,
        headroom_proxy_url: str,
        clock: Callable[[], datetime] | None = None,
        policy_version: str = "v1",
    ) -> None:
        if not headroom_proxy_url:
            raise ValueError("headroom_proxy_url must not be blank")
        if not policy_version:
            raise ValueError("policy_version must not be blank")
        self.store = store
        self.journals = journals
        self.assembler = assembler
        self.compression_queue = compression_queue
        self.policy = policy
        self.scope_factory = scope_factory
        self.settings = settings
        self.token_estimator = token_estimator
        self.headroom_proxy_url = headroom_proxy_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy_version = policy_version

    async def write(
        self, request: MemoryWriteRequest, request_id: str
    ) -> MemoryWriteResponse:
        """Journal each original before its idempotent Redis commit."""

        started = time.perf_counter()
        redis_seconds = 0.0
        journal_seconds = 0.0
        queue_seconds = 0.0
        sequences: list[int] = []
        duplicate_event_ids: list[str] = []
        committed_event_ids: list[str] = []

        for input_event in request.events:
            digest = sha256(input_event.content.encode("utf-8")).hexdigest()
            redis_started = time.perf_counter()
            reservation = await self.store.reserve_event(
                request.user_id, request.session_id, input_event.event_id, digest
            )
            redis_seconds += time.perf_counter() - redis_started
            sequences.append(reservation.sequence)

            if reservation.state == "committed":
                duplicate_event_ids.append(input_event.event_id)
                continue

            event = MemoryEvent(
                sequence=reservation.sequence,
                event_id=input_event.event_id,
                role=input_event.role,
                content_type=input_event.content_type,
                content=input_event.content,
                metadata=input_event.metadata,
                sha256=digest,
                created_at=self._now().isoformat(),
            )
            journal_started = time.perf_counter()
            try:
                await anyio.to_thread.run_sync(
                    self.journals.append_event,
                    request.user_id,
                    request.session_id,
                    event,
                )
            except JournalConflictError as error:
                raise EventConflictError(str(error)) from error
            journal_seconds += time.perf_counter() - journal_started

            redis_started = time.perf_counter()
            try:
                committed = await self.store.commit_event(
                    request.user_id, request.session_id, event
                )
            except Exception as error:
                raise RetryableWriteError(
                    input_event.event_id, tuple(committed_event_ids)
                ) from error
            redis_seconds += time.perf_counter() - redis_started
            if committed == "duplicate":
                duplicate_event_ids.append(input_event.event_id)
            else:
                committed_event_ids.append(input_event.event_id)

        originals = await self.store.read_originals_after(
            request.user_id, request.session_id, 0
        )
        envelope = await self.store.read_envelope(request.user_id, request.session_id)
        should_compress = self.policy.should_compress(
            estimated_tokens=self._estimate_tokens(originals),
            message_count=len(originals),
            session_seconds=request.session_seconds,
        )
        if should_compress and originals:
            queue_started = time.perf_counter()
            await self.compression_queue.enqueue(
                self._compression_job(
                    request.user_id,
                    request.session_id,
                    envelope,
                    originals[-1].sequence,
                    rebuild=False,
                )
            )
            queue_seconds += time.perf_counter() - queue_started

        return MemoryWriteResponse(
            request_id=request_id,
            accepted=True,
            sequence_from=min(sequences) if sequences else None,
            sequence_through=max(sequences) if sequences else None,
            duplicate_event_ids=duplicate_event_ids,
            compression_queued=should_compress and bool(originals),
            policy_version=self.policy_version,
            timing_ms=WriteTiming(
                total=self._milliseconds(started),
                redis=redis_seconds * 1_000,
                journal=journal_seconds * 1_000,
                queue=queue_seconds * 1_000,
            ),
        )

    async def read(
        self, request: MemoryReadRequest, request_id: str
    ) -> MemoryReadResponse:
        """Assemble Redis context, recovering originals from the journal if needed."""

        started = time.perf_counter()
        redis_started = time.perf_counter()
        history_turns = request.history_turns or self.settings.redis_session.history_turns
        envelope, originals = await asyncio.gather(
            self.store.read_envelope(request.user_id, request.session_id),
            self.store.read_recent_originals(
                request.user_id, request.session_id, history_turns
            ),
        )
        redis_seconds = time.perf_counter() - redis_started
        recovery_seconds = 0.0
        source = "redis"

        if not originals:
            recovery_started = time.perf_counter()
            originals = await anyio.to_thread.run_sync(
                self._recent_journal_originals,
                request.user_id,
                request.session_id,
                history_turns,
            )
            if originals:
                await self._restore_originals(
                    request.user_id, request.session_id, originals
                )
            recovery_seconds = time.perf_counter() - recovery_started
            source = "journal_rebuild"

        now = self._now()
        latest_sequence = max((event.sequence for event in originals), default=0)
        if self._requires_rebuild(envelope, now):
            if latest_sequence:
                await self.compression_queue.enqueue(
                    self._compression_job(
                        request.user_id,
                        request.session_id,
                        envelope,
                        latest_sequence,
                        rebuild=True,
                    )
                )
            source = "journal_rebuild"

        assembly_started = time.perf_counter()
        messages = self.assembler.build_read_messages(envelope, originals, now)
        assembly_seconds = time.perf_counter() - assembly_started
        scope = self.scope_factory.for_session(request.user_id, request.session_id)
        config = self._effective_config() if request.include_effective_config else None

        return MemoryReadResponse(
            request_id=request_id,
            messages=list(messages),
            memory=MemoryReadState(
                compressed_through_sequence=(
                    envelope.compressed_through_sequence if envelope is not None else 0
                ),
                latest_sequence=latest_sequence,
                source=source,
                compression_segments=(
                    len(envelope.compression_generations) if envelope is not None else 0
                ),
            ),
            headroom=HeadroomProxyContext(
                proxy_url=self.headroom_proxy_url,
                scope_headers=scope.as_headroom_headers(),
            ),
            effective_config=config,
            timing_ms=ReadTiming(
                total=self._milliseconds(started),
                redis=redis_seconds * 1_000,
                recovery=recovery_seconds * 1_000,
                assembly=assembly_seconds * 1_000,
            ),
        )

    def _recent_journal_originals(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[MemoryEvent, ...]:
        originals = self.journals.read_original_range(
            user_id, session_id, 1, 2**63 - 1
        )
        return originals[-history_turns:]

    async def _restore_originals(
        self, user_id: str, session_id: str, originals: tuple[MemoryEvent, ...]
    ) -> None:
        """Replay journal originals without changing their journal records."""

        restore = getattr(self.store, "restore_originals", None)
        if restore is not None:
            await restore(user_id, session_id, originals)
            return
        for original in originals:
            reservation = await self.store.reserve_event(
                user_id, session_id, original.event_id, original.sha256
            )
            recovered = original.model_copy(update={"sequence": reservation.sequence})
            await self.store.commit_event(user_id, session_id, recovered)

    def _compression_job(
        self,
        user_id: str,
        session_id: str,
        envelope: MemorySummaryEnvelope | None,
        through_sequence: int,
        *,
        rebuild: bool,
    ) -> CompressionJob:
        expected_version = envelope.version if envelope is not None else 0
        job_identity = f"{user_id}\n{session_id}\n{expected_version}\n{through_sequence}\n{rebuild}"
        return CompressionJob(
            job_id=f"memory-{uuid5(NAMESPACE_URL, job_identity).hex}",
            user_id=user_id,
            session_id=session_id,
            expected_version=expected_version,
            requested_through_sequence=through_sequence,
        )

    def _effective_config(self) -> EffectiveMemoryConfig:
        return EffectiveMemoryConfig(
            history_turns=self.settings.redis_session.history_turns,
            redis_ttl_seconds=self.settings.redis_session.ttl_seconds,
            ccr_ttl_seconds=self.settings.headroom_service.ccr_ttl_seconds,
            journal_retention_days=self.settings.journal.retention_days,
            trigger_ratio=self.settings.redis_session.trigger_ratio,
            policy_version=self.policy_version,
        )

    def _estimate_tokens(self, originals: tuple[MemoryEvent, ...]) -> int:
        messages = tuple(
            {"role": event.role.value, "content": event.content} for event in originals
        )
        estimator = self.token_estimator
        if hasattr(estimator, "estimate"):
            return int(estimator.estimate(messages))
        return int(estimator(messages))

    def _requires_rebuild(
        self, envelope: MemorySummaryEnvelope | None, now: datetime
    ) -> bool:
        if envelope is None:
            return False
        refresh_at = now + timedelta(
            seconds=self.settings.headroom_service.ccr_refresh_seconds
        )
        return any(
            self._aware_datetime(generation.ccr_expires_at) <= refresh_at
            for generation in envelope.compression_generations
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _aware_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("CCR timestamps must be timezone-aware")
        return parsed

    @staticmethod
    def _milliseconds(started: float) -> float:
        return (time.perf_counter() - started) * 1_000
