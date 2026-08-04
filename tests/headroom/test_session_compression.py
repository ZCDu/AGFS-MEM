from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any, Mapping

from dream.memory.session_compression import (
    ExecutorHeadroomCompressionQueue,
    HeadroomCompressionJob,
    HeadroomCompressionResult,
    HeadroomCompressionStatus,
    HeadroomFailureReason,
    SessionSummaryGenerator,
    SessionSummaryPayload,
)


NOW = datetime(2026, 7, 31, 1, 2, 3, tzinfo=timezone.utc)
MESSAGES = ({"role": "user", "content": "original"},)


def summary_payload(
    *,
    attachment: bool = False,
) -> SessionSummaryPayload:
    value: dict[str, object] = {
        "current_goal": ["完成 DREAM 短期记忆"],
        "preferences": ["优先本地 Python"],
        "confirmed_facts": ["journals 保存完整原文"],
        "pending_items": ["运行真实 Redis 测试"],
        "attachment_references": [],
    }
    if attachment:
        value["attachment_references"] = [
            {
                "placeholder": "[attachment: raw/plan.pdf]",
                "raw_ref": "raw/plan.pdf",
                "source_ref": None,
            }
        ]
    return SessionSummaryPayload.model_validate(value)


class RecordingCompletions:
    def __init__(self, contents: list[object]) -> None:
        self.contents = contents
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def test_summary_generator_returns_only_five_plan_categories() -> None:
    payload = summary_payload(attachment=True).model_dump()
    completions = RecordingCompletions([json.dumps(payload, ensure_ascii=False)])
    generator = SessionSummaryGenerator(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="summary-model",
    )
    messages = (
        {"role": "user", "content": "compressed goal"},
        {"role": "system", "content": "[attachment: raw/plan.pdf]"},
    )

    summary = generator.summarize(messages)

    assert summary == summary_payload(attachment=True)
    request = completions.requests[0]
    assert request["model"] == "summary-model"
    assert request["response_format"] == {"type": "json_object"}
    assert "compressed goal" in request["messages"][1]["content"]
    assert set(summary.model_dump()) == {
        "current_goal",
        "preferences",
        "confirmed_facts",
        "pending_items",
        "attachment_references",
    }


def test_summary_generator_repairs_invalid_structured_output_once() -> None:
    valid = json.dumps(summary_payload().model_dump(), ensure_ascii=False)
    completions = RecordingCompletions(["not json", valid])
    generator = SessionSummaryGenerator(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model="summary-model",
    )

    assert generator.summarize(MESSAGES) == summary_payload()
    assert len(completions.requests) == 2
    assert "validation_feedback" in completions.requests[1]["messages"][1]["content"]


def success_result(
    messages: tuple[dict[str, object], ...] = (
        {"role": "user", "content": "compressed"},
    ),
) -> HeadroomCompressionResult:
    return HeadroomCompressionResult(
        status=HeadroomCompressionStatus.SUCCESS,
        messages=messages,
        fallback_used=False,
        compression_applied=True,
        tokens_before=100,
        tokens_after=25,
        tokens_saved=75,
    )


def failed_result(
    *,
    messages: tuple[dict[str, str], ...],
    fallback_used: bool,
) -> HeadroomCompressionResult:
    return HeadroomCompressionResult(
        status=HeadroomCompressionStatus.FAILED,
        messages=messages,
        fallback_used=fallback_used,
        failure_reason=HeadroomFailureReason.UNEXPECTED_ERROR,
    )


@dataclass(frozen=True)
class CompressionCall:
    messages: tuple[dict[str, Any], ...]
    model: str
    correlation_id: str | None
    scope_headers: Mapping[str, str] | None


class FixedCompressionClient:
    def __init__(self, result: HeadroomCompressionResult) -> None:
        self.result = result
        self.calls: list[CompressionCall] = []

    def compress(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        model: str,
        correlation_id: str | None = None,
        scope_headers: Mapping[str, str] | None = None,
    ) -> HeadroomCompressionResult:
        self.calls.append(
            CompressionCall(messages, model, correlation_id, scope_headers)
        )
        return self.result


class RecordingSummaryModel:
    def __init__(self, summary: SessionSummaryPayload) -> None:
        self.summary = summary
        self.inputs: list[tuple[dict[str, str], ...]] = []

    def summarize(
        self, messages: tuple[dict[str, str], ...]
    ) -> SessionSummaryPayload:
        self.inputs.append(messages)
        return self.summary


def test_job_uses_scope_headers_for_the_same_user_and_session() -> None:
    client = FixedCompressionClient(success_result())
    job = HeadroomCompressionJob(
        store=RecordingSummaryStore(),
        compression_client=client,
        summary_model=RecordingSummaryModel(summary_payload()),
        compression_model="gpt-4o",
        scope_headers_factory=lambda user_id, session_id: {
            "x-headroom-user-id": f"u:{user_id}",
            "x-headroom-session-id": f"s:{session_id}",
            "x-headroom-project-id": f"p:{session_id}",
        },
        clock=lambda: NOW,
    )

    job.run("user", "session", MESSAGES, 2, 1)

    assert client.calls[0].scope_headers is not None
    assert (
        client.calls[0].scope_headers["x-headroom-session-id"]
        == "s:session"
    )


class FailingSummaryModel:
    def summarize(
        self, messages: tuple[dict[str, str], ...]
    ) -> SessionSummaryPayload:
        raise RuntimeError("private model response")


class RecordingSummaryStore:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str, int, int]] = []

    def store_compression_result(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None:
        self.results.append(
            (user_id, session_id, summary, processed_message_count, keep_recent_turns)
        )


@dataclass(frozen=True)
class RetryCall:
    user_id: str
    session_id: str
    failure_stage: str
    failure_reason: str


class RecordingRetryQueue:
    def __init__(self) -> None:
        self.calls: list[RetryCall] = []

    def schedule(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, str], ...],
        processed_message_count: int,
        keep_recent_turns: int,
        failure_stage: str,
        failure_reason: str,
    ) -> None:
        self.calls.append(
            RetryCall(user_id, session_id, failure_stage, failure_reason)
        )


def test_success_stores_dream_owned_structured_redis_summary() -> None:
    store = RecordingSummaryStore()
    model = RecordingSummaryModel(summary_payload())
    job = HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(success_result()),
        summary_model=model,
        compression_model="gpt-4o",
        clock=lambda: NOW,
    )

    result = job.run("user", "session", MESSAGES, 4, 1)

    stored = json.loads(store.results[0][2])
    assert stored["user_id"] == "user"
    assert stored["session_id"] == "session"
    assert stored["coverage"] == {"processed_message_count": 4}
    assert stored["updated_at"] == NOW.isoformat()
    assert stored["current_goal"] == ["完成 DREAM 短期记忆"]
    assert "user_id" not in model.summary.model_dump()
    assert result.summary_written is True


def test_job_stores_headroom_messages_unchanged_without_reference_index() -> None:
    marker = "[500 items compressed to 15. Retrieve more: hash=abc123]"
    compression = success_result(
        messages=(
            {
                "role": "tool",
                "tool_call_id": "call_search",
                "content": marker,
            },
        ),
    )
    store = RecordingSummaryStore()
    model = RecordingSummaryModel(summary_payload())

    HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(compression),
        summary_model=model,
        compression_model="gpt-4o",
        clock=lambda: NOW,
    ).run("user", "session", MESSAGES, 4, 1)

    context = json.loads(store.results[0][2])["compression_context"]
    assert context == {
        "messages": [
            {
                "role": "tool",
                "content": marker,
                "tool_call_id": "call_search",
            }
        ],
        "tokens_before": 100,
        "tokens_after": 25,
    }
    assert marker in json.dumps(model.inputs, ensure_ascii=False)


def test_noop_does_not_duplicate_original_transcript_in_envelope() -> None:
    compression = HeadroomCompressionResult(
        status=HeadroomCompressionStatus.SUCCESS,
        messages=MESSAGES,
        fallback_used=False,
        compression_applied=False,
        tokens_before=10,
        tokens_after=10,
        tokens_saved=0,
    )
    store = RecordingSummaryStore()

    HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(compression),
        summary_model=RecordingSummaryModel(summary_payload()),
        compression_model="gpt-4o",
        clock=lambda: NOW,
    ).run("user", "session", MESSAGES, 2, 1)

    context = json.loads(store.results[0][2])["compression_context"]
    assert context["messages"] == []


def test_attachment_reference_must_exist_in_summary_input() -> None:
    store = RecordingSummaryStore()
    retry = RecordingRetryQueue()
    job = HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(success_result()),
        summary_model=RecordingSummaryModel(summary_payload(attachment=True)),
        compression_model="gpt-4o",
        retry_queue=retry,
        clock=lambda: NOW,
    )

    result = job.run("user", "session", MESSAGES, 4, 1)

    assert result.summary_written is False
    assert store.results == []
    assert retry.calls == [
        RetryCall("user", "session", "summary", "invalid_response")
    ]


def test_noop_and_development_fallback_still_generate_summary() -> None:
    for compression in (
        HeadroomCompressionResult(
            status=HeadroomCompressionStatus.SUCCESS,
            messages=MESSAGES,
            fallback_used=False,
            transforms_applied=("router:noop",),
            tokens_before=100,
            tokens_after=100,
            tokens_saved=0,
        ),
        failed_result(messages=MESSAGES, fallback_used=True),
    ):
        store = RecordingSummaryStore()
        model = RecordingSummaryModel(summary_payload())
        result = HeadroomCompressionJob(
            store=store,
            compression_client=FixedCompressionClient(compression),
            summary_model=model,
            compression_model="gpt-4o",
            clock=lambda: NOW,
        ).run("user", "session", MESSAGES, 2, 1)

        assert result.summary_written is True
        assert model.inputs == [MESSAGES]
        assert json.loads(store.results[0][2])["session_id"] == "session"


def test_production_compression_failure_does_not_summarize_store_or_trim() -> None:
    retry = RecordingRetryQueue()
    store = RecordingSummaryStore()
    model = RecordingSummaryModel(summary_payload())
    result = HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(
            failed_result(messages=(), fallback_used=False)
        ),
        summary_model=model,
        compression_model="gpt-4o",
        retry_queue=retry,
        clock=lambda: NOW,
    ).run("user", "session", MESSAGES, 4, 1)

    assert result.summary_written is False
    assert model.inputs == []
    assert store.results == []
    assert retry.calls == [
        RetryCall("user", "session", "compression", "unexpected_error")
    ]


def test_summary_failure_is_safe_and_scheduled_without_store() -> None:
    retry = RecordingRetryQueue()
    store = RecordingSummaryStore()
    result = HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(success_result()),
        summary_model=FailingSummaryModel(),
        compression_model="gpt-4o",
        retry_queue=retry,
        clock=lambda: NOW,
    ).run("user", "session", MESSAGES, 4, 1)

    assert result.summary_written is False
    assert result.failure_reason == "summary_failed"
    assert store.results == []
    assert retry.calls == [
        RetryCall("user", "session", "summary", "RuntimeError")
    ]


class RecordingExecutor:
    def __init__(self) -> None:
        self.submissions: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> object:
        self.submissions.append((function, args))
        return SimpleNamespace()


def test_executor_queue_defers_compression_summary_and_store_work() -> None:
    store = RecordingSummaryStore()
    job = HeadroomCompressionJob(
        store=store,
        compression_client=FixedCompressionClient(success_result()),
        summary_model=RecordingSummaryModel(summary_payload()),
        compression_model="gpt-4o",
        clock=lambda: NOW,
    )
    executor = RecordingExecutor()
    queue = ExecutorHeadroomCompressionQueue(job, executor)

    queue.enqueue("user", "session", MESSAGES, 2, 1)

    assert store.results == []
    function, args = executor.submissions[0]
    function(*args)
    assert json.loads(store.results[0][2])["coverage"] == {
        "processed_message_count": 2
    }
