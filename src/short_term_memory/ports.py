"""Replaceable ports used by the short-term memory SDK."""

from typing import Any, Callable, Literal, Mapping, Protocol

from short_term_memory.models import (
    HeadroomCompressionResult,
    SessionSummaryPayload,
)


class TokenEstimator(Protocol):
    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int: ...


class CompressionClient(Protocol):
    def compress(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        model: str,
        correlation_id: str | None = None,
        scope_headers: Mapping[str, str] | None = None,
    ) -> HeadroomCompressionResult: ...


class SummaryModel(Protocol):
    def summarize(
        self, messages: tuple[dict[str, Any], ...]
    ) -> SessionSummaryPayload: ...


class BackgroundExecutor(Protocol):
    def submit(self, function: Callable[..., object], *args: object) -> object: ...


class RetryQueue(Protocol):
    def schedule(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
        failure_stage: Literal["compression", "summary"],
        failure_reason: str,
    ) -> None: ...


class SessionSummaryStore(Protocol):
    def store_compression_result(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None: ...


class SessionCompressionQueue(Protocol):
    def enqueue(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, Any], ...],
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None: ...


class SummarySnapshotReader(Protocol):
    def read(self, user_id: str, session_id: str) -> str | None: ...
