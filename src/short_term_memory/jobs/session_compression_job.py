"""Background Headroom compression and DREAM-owned session summaries."""

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from short_term_memory.compression.summary import (
    summary_input,
    validate_attachment_references,
)
from short_term_memory.models import (
    HeadroomCompressionStatus,
    HeadroomJobResult,
    SessionCompressionContext,
    SessionSummaryCoverage,
    SessionSummaryDocument,
    SessionSummaryPayload,
)
from short_term_memory.ports import (
    BackgroundExecutor,
    CompressionClient,
    RetryQueue,
    SessionSummaryStore,
    SummaryModel,
)


class SessionCompressionJob:
    """Compress an eligible session, summarize it, and persist the Redis view."""

    def __init__(
        self,
        *,
        store: SessionSummaryStore,
        compression_client: CompressionClient,
        summary_model: SummaryModel,
        compression_model: str,
        retry_queue: RetryQueue | None = None,
        clock: Callable[[], datetime] | None = None,
        scope_headers_factory: (
            Callable[[str, str], Mapping[str, str]] | None
        ) = None,
    ) -> None:
        self.store = store
        self.compression_client = compression_client
        self.summary_model = summary_model
        self.compression_model = compression_model
        self.retry_queue = retry_queue
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.scope_headers_factory = scope_headers_factory

    def run(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
        correlation_id: str | None = None,
    ) -> HeadroomJobResult:
        if self.scope_headers_factory is None:
            compression = self.compression_client.compress(
                messages,
                model=self.compression_model,
                correlation_id=correlation_id,
            )
        else:
            compression = self.compression_client.compress(
                messages,
                model=self.compression_model,
                correlation_id=correlation_id,
                scope_headers=self.scope_headers_factory(user_id, session_id),
            )
        if (
            compression.status is HeadroomCompressionStatus.FAILED
            and not compression.fallback_used
        ):
            if self.retry_queue is not None and compression.failure_reason is not None:
                self.retry_queue.schedule(
                    user_id,
                    session_id,
                    messages,
                    processed_message_count,
                    keep_recent_turns,
                    "compression",
                    compression.failure_reason.value,
                )
            return HeadroomJobResult(
                compression_status=compression.status,
                fallback_used=False,
                summary_written=False,
                compression_applied=False,
                failure_reason=compression.failure_reason,
            )

        model_input = summary_input(compression.messages, messages)
        try:
            summary = self.summary_model.summarize(model_input)
            if not isinstance(summary, SessionSummaryPayload):
                summary = SessionSummaryPayload.model_validate(summary)
            validate_attachment_references(summary, model_input)
        except Exception as exc:
            if self.retry_queue is not None:
                self.retry_queue.schedule(
                    user_id,
                    session_id,
                    messages,
                    processed_message_count,
                    keep_recent_turns,
                    "summary",
                    (
                        "invalid_response"
                        if isinstance(exc, (ValueError, TypeError))
                        else type(exc).__name__
                    ),
                )
            return HeadroomJobResult(
                compression_status=compression.status,
                fallback_used=compression.fallback_used,
                summary_written=False,
                compression_applied=compression.compression_applied,
                failure_reason="summary_failed",
            )

        updated_at = self.clock()
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("summary clock must return a timezone-aware datetime")
        document = SessionSummaryDocument(
            user_id=user_id,
            session_id=session_id,
            coverage=SessionSummaryCoverage(
                processed_message_count=processed_message_count
            ),
            compression_context=SessionCompressionContext(
                messages=(
                    list(compression.messages)
                    if compression.compression_applied
                    else []
                ),
                tokens_before=compression.tokens_before,
                tokens_after=compression.tokens_after,
            ),
            updated_at=updated_at.isoformat(),
            **summary.model_dump(),
        )
        self.store.store_compression_result(
            user_id,
            session_id,
            document.model_dump_json(),
            processed_message_count,
            keep_recent_turns,
        )
        return HeadroomJobResult(
            compression_status=compression.status,
            fallback_used=compression.fallback_used,
            summary_written=True,
            compression_applied=compression.compression_applied,
            failure_reason=compression.failure_reason,
        )


class ExecutorSessionCompressionQueue:
    """Submit compression and summary work without blocking the online path."""

    def __init__(
        self,
        job: SessionCompressionJob,
        executor: BackgroundExecutor,
    ) -> None:
        self.job = job
        self.executor = executor

    def enqueue(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None:
        self.executor.submit(
            self.job.run,
            user_id,
            session_id,
            messages,
            processed_message_count,
            keep_recent_turns,
        )
