from datetime import datetime, timezone
from pathlib import Path

from dream.api.conversation_handler import (
    ConversationHandler,
    HeadroomPolicy,
    RedisSessionContext,
)
from dream.api.optimization_scope import OptimizationScopeFactory
from short_term_memory.storage.journal_store import JournalMessageEvent, JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter

from tests.storage.fake_redis import FakeRedis


NOW = datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc)


class FixedTokenEstimator:
    def __init__(self, value: int) -> None:
        self.value = value

    def estimate(self, messages: tuple[dict[str, str], ...]) -> int:
        return self.value


class RecordingHeadroomQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def enqueue(
        self,
        user_id: str,
        session_id: str,
        messages: tuple[dict[str, str], ...],
        processed_message_count: int,
        keep_recent_turns: int,
    ) -> None:
        self.calls.append(
            (
                user_id,
                session_id,
                messages,
                processed_message_count,
                keep_recent_turns,
            )
        )


def _handler(
    tmp_path: Path,
    *,
    estimated_tokens: int = 1,
    history_turns: int = 10,
    recovery_queue: RecordingHeadroomQueue | None = None,
):
    vfs = VFSAdapter(tmp_path)
    journals = JournalStore(vfs)
    context = RedisSessionContext(
        FakeRedis(),
        journals,
        recovery_queue=recovery_queue,
    )
    headroom_queue = RecordingHeadroomQueue()
    handler = ConversationHandler(
        session_context=context,
        journal_store=journals,
        headroom_policy=HeadroomPolicy(
            context_window_tokens=100,
            trigger_ratio=0.65,
            max_messages=20,
            max_session_seconds=3_600,
        ),
        token_estimator=FixedTokenEstimator(estimated_tokens),
        headroom_queue=headroom_queue,
        history_turns=history_turns,
        optimization_scope_factory=OptimizationScopeFactory("test-secret"),
        headroom_proxy_url="http://127.0.0.1:8787/v1",
    )
    return vfs, journals, context, headroom_queue, handler


def test_agent_calls_prepare_before_answer_and_complete_after_answer(
    tmp_path: Path,
) -> None:
    _, journals, context, _, handler = _handler(tmp_path)

    prepared = handler.prepare_turn(
        "user-1", "sess-1", "hello", timestamp=NOW, session_seconds=120
    )
    assert prepared.history == ({"role": "user", "content": "hello"},)

    completed = handler.complete_turn(
        prepared,
        assistant_content="answer produced by company Agent",
    )

    assert completed.headroom_queued is False
    assert context.recent_messages("user-1", "sess-1", 10)[-1] == {
        "role": "assistant",
        "content": "answer produced by company Agent",
    }
    assert [
        record.role
        for record in journals.read_session("user-1", "sess-1")
        if isinstance(record, JournalMessageEvent)
    ] == ["user", "assistant"]


def test_prepare_turn_restores_expired_history_before_current_user_write(
    tmp_path: Path,
) -> None:
    _, journals, _, _, handler = _handler(tmp_path)
    journals.append_message(
        "user-1",
        "sess-1",
        role="user",
        content="historical question",
        timestamp=NOW,
    )
    journals.append_message(
        "user-1",
        "sess-1",
        role="assistant",
        content="historical answer",
        timestamp=NOW,
    )

    prepared = handler.prepare_turn(
        "user-1", "sess-1", "current question", timestamp=NOW
    )

    assert prepared.history == (
        {"role": "user", "content": "historical question"},
        {"role": "assistant", "content": "historical answer"},
        {"role": "user", "content": "current question"},
    )


def test_prepare_turn_queues_old_history_rebuild_without_running_it_inline(
    tmp_path: Path,
) -> None:
    recovery_queue = RecordingHeadroomQueue()
    _, journals, _, _, handler = _handler(
        tmp_path,
        history_turns=1,
        recovery_queue=recovery_queue,
    )
    for index in range(2):
        journals.append_message(
            "user-1",
            "sess-1",
            role="user",
            content=f"question-{index}",
            timestamp=NOW,
        )
        journals.append_message(
            "user-1",
            "sess-1",
            role="assistant",
            content=f"answer-{index}",
            timestamp=NOW,
        )

    prepared = handler.prepare_turn(
        "user-1",
        "sess-1",
        "current question",
        timestamp=NOW,
    )

    assert prepared.history == (
        {"role": "user", "content": "question-1"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "current question"},
    )
    assert len(recovery_queue.calls) == 1
    assert recovery_queue.calls[0][2] == (
        {"role": "user", "content": "question-0"},
        {"role": "assistant", "content": "answer-0"},
    )


def test_user_and_assistant_messages_are_written_to_redis_and_journals(
    tmp_path: Path,
) -> None:
    _, journals, context, _, handler = _handler(tmp_path)

    prepared = handler.prepare_turn("user-1", "sess-1", "hello", timestamp=NOW)
    handler.complete_turn(prepared, assistant_content="fixed answer")
    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "fixed answer"},
    )
    records = journals.read_session("user-1", "sess-1")
    assert [record.role for record in records if isinstance(record, JournalMessageEvent)] == [
        "user",
        "assistant",
    ]


def test_answer_history_uses_redis_summary_and_recent_messages(
    tmp_path: Path,
) -> None:
    _, _, context, _, handler = _handler(tmp_path)
    context.append_message("user-1", "sess-1", {"role": "user", "content": "old"})
    context.set_summary("user-1", "sess-1", "earlier context")

    prepared = handler.prepare_turn("user-1", "sess-1", "new", timestamp=NOW)

    assert prepared.history == (
        {"role": "system", "content": "earlier context"},
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    )


def test_prepare_turn_returns_scope_without_inline_compression(
    tmp_path: Path,
) -> None:
    _, _, context, queue, handler = _handler(
        tmp_path, estimated_tokens=100_000
    )
    context.set_summary("user-1", "sess-1", "stable session summary")

    prepared = handler.prepare_turn(
        "user-1", "sess-1", "long request", timestamp=NOW
    )

    assert prepared.history[-1] == {
        "role": "user",
        "content": "long request",
    }
    assert prepared.optimization_scope == (
        handler.optimization_scope_factory.for_session("user-1", "sess-1")
    )
    assert queue.calls == []


def test_prepare_turn_exposes_proxy_route_without_calling_a_model(
    tmp_path: Path,
) -> None:
    _, _, _, queue, handler = _handler(tmp_path, estimated_tokens=100_000)

    prepared = handler.prepare_turn(
        "user-1", "sess-1", "long request", timestamp=NOW
    )

    assert prepared.headroom_proxy_url == "http://127.0.0.1:8787/v1"
    assert (
        prepared.headroom_headers
        == prepared.optimization_scope.as_headroom_headers()
    )
    assert prepared.history[-1] == {
        "role": "user",
        "content": "long request",
    }
    assert queue.calls == []
    assert not hasattr(handler, "answer_generator")


def test_complete_turn_still_queues_background_compression(
    tmp_path: Path,
) -> None:
    _, _, _, queue, handler = _handler(tmp_path, estimated_tokens=65)

    prepared = handler.prepare_turn(
        "user-1", "sess-1", "question", timestamp=NOW
    )
    result = handler.complete_turn(prepared, assistant_content="answer")

    assert result.headroom_queued is True
    assert len(queue.calls) == 1


def test_duration_trigger_remains_post_turn_and_does_not_block_prepare(
    tmp_path: Path,
) -> None:
    _, _, _, background_queue, handler = _handler(
        tmp_path,
        estimated_tokens=1,
    )

    prepared = handler.prepare_turn(
        "user-1",
        "sess-1",
        "normal request",
        timestamp=NOW,
        session_seconds=3_600,
    )
    completed = handler.complete_turn(
        prepared, assistant_content="original assistant answer"
    )

    assert completed.headroom_queued is True
    assert len(background_queue.calls) == 1


def test_headroom_message_threshold_counts_full_redis_session(
    tmp_path: Path,
) -> None:
    vfs = VFSAdapter(tmp_path)
    journals = JournalStore(vfs)
    context = RedisSessionContext(FakeRedis(), journals)
    context.append_message("user-1", "sess-1", {"role": "user", "content": "old-1"})
    context.append_message("user-1", "sess-1", {"role": "assistant", "content": "old-2"})
    headroom_queue = RecordingHeadroomQueue()
    handler = ConversationHandler(
        session_context=context,
        journal_store=journals,
        headroom_policy=HeadroomPolicy(1_000, 0.65, 3, 3_600),
        token_estimator=FixedTokenEstimator(1),
        headroom_queue=headroom_queue,
        history_turns=1,
        optimization_scope_factory=OptimizationScopeFactory("test-secret"),
    )

    prepared = handler.prepare_turn("user-1", "sess-1", "new", timestamp=NOW)
    handler.complete_turn(prepared, assistant_content="fixed answer")

    assert prepared.history == (
        {"role": "user", "content": "old-1"},
        {"role": "assistant", "content": "old-2"},
        {"role": "user", "content": "new"},
    )
    assert len(headroom_queue.calls[0][2]) == 4


def test_headroom_compression_is_queued_without_summary_or_journal_write(
    tmp_path: Path,
) -> None:
    _, journals, context, headroom_queue, handler = _handler(
        tmp_path, estimated_tokens=65
    )

    prepared = handler.prepare_turn("user-1", "sess-1", "compress", timestamp=NOW)
    result = handler.complete_turn(
        prepared,
        assistant_content="fixed answer",
    )

    assert result.headroom_queued is True
    assert len(headroom_queue.calls) == 1
    assert headroom_queue.calls[0][:2] == ("user-1", "sess-1")
    assert context.get_summary("user-1", "sess-1") is None
    assert all(
        record.content != "headroom summary"
        for record in journals.read_session("user-1", "sess-1")
        if isinstance(record, JournalMessageEvent)
    )
