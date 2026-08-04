"""Composition boundary for Redis and replaceable Headroom short-term memory."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from dream.api.conversation_handler import (
    ConversationHandler,
    HeadroomPolicy,
    RedisClient,
    RedisSessionContext,
    TokenEstimator,
)
from dream.api.optimization_scope import OptimizationScopeFactory
from dream.config import DreamSettings
from dream.integrations.headroom_client import HeadroomHttpClient
from dream.integrations.headroom_telemetry import (
    HeadroomTelemetry,
    InMemoryHeadroomTelemetry,
)
from dream.memory.session_compression import (
    BackgroundExecutor,
    CompressionClient,
    ExecutorHeadroomCompressionQueue,
    HeadroomCompressionJob,
    HeadroomRetryQueue,
    SummaryModel,
)
from dream.storage.journal_store import JournalStore
from dream.storage.vfs_adapter import VFSAdapter


@dataclass(frozen=True)
class ShortTermMemoryRuntime:
    session_context: RedisSessionContext
    conversation_handler: ConversationHandler
    compression_client: CompressionClient
    compression_job: HeadroomCompressionJob
    compression_queue: ExecutorHeadroomCompressionQueue
    telemetry: HeadroomTelemetry


def build_short_term_runtime(
    *,
    home: Path,
    settings: DreamSettings,
    redis_client: RedisClient,
    token_estimator: TokenEstimator,
    summary_model: SummaryModel,
    executor: BackgroundExecutor,
    retry_queue: HeadroomRetryQueue,
    compression_client: CompressionClient | None = None,
    telemetry: HeadroomTelemetry | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ShortTermMemoryRuntime:
    """Wire the Agent-facing short-term boundary without retrieval or answering."""

    metrics = telemetry or InMemoryHeadroomTelemetry()
    journals = JournalStore(VFSAdapter(home))
    headroom = settings.headroom_service
    context = RedisSessionContext(
        redis_client,
        journals,
        ttl_seconds=settings.redis_session.ttl_seconds,
        telemetry=metrics,
        ccr_ttl_seconds=headroom.ccr_ttl_seconds,
        clock=clock,
    )
    selected_client = compression_client or HeadroomHttpClient(
        service_url=headroom.url or None,
        environment=settings.environment,
        timeout_seconds=headroom.timeout_seconds,
        telemetry=metrics,
    )
    scope_factory = OptimizationScopeFactory(
        settings.optimization_scope_secret,
        telemetry=metrics,
    )
    compression_job = HeadroomCompressionJob(
        store=context,
        compression_client=selected_client,
        summary_model=summary_model,
        compression_model=headroom.compression_model,
        retry_queue=retry_queue,
        scope_headers_factory=lambda user_id, session_id: (
            scope_factory.for_session(user_id, session_id).as_headroom_headers()
        ),
    )
    compression_queue = ExecutorHeadroomCompressionQueue(
        compression_job,
        executor,
    )
    context.recovery_queue = compression_queue
    proxy_url = f"{headroom.url}/v1" if headroom.url else None
    handler = ConversationHandler(
        session_context=context,
        journal_store=journals,
        headroom_policy=HeadroomPolicy(
            context_window_tokens=(
                settings.redis_session.context_window_tokens
            ),
            trigger_ratio=settings.redis_session.trigger_ratio,
            max_messages=settings.redis_session.max_messages,
            max_session_seconds=settings.redis_session.max_session_seconds,
        ),
        token_estimator=token_estimator,
        headroom_queue=compression_queue,
        history_turns=settings.redis_session.history_turns,
        optimization_scope_factory=scope_factory,
        headroom_proxy_url=proxy_url,
    )
    return ShortTermMemoryRuntime(
        session_context=context,
        conversation_handler=handler,
        compression_client=selected_client,
        compression_job=compression_job,
        compression_queue=compression_queue,
        telemetry=metrics,
    )
