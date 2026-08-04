from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from dream.api.conversation_handler import HeadroomPolicy, RedisSessionContext
from dream.integrations.headroom_telemetry import InMemoryHeadroomTelemetry
from dream.storage.journal_store import JournalStore
from dream.storage.vfs_adapter import VFSAdapter
from tests.api.fake_redis import FakeRedis


NOW = datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc)


def _context(
    tmp_path: Path,
    client: FakeRedis | None = None,
    **context_kwargs: object,
):
    vfs = VFSAdapter(tmp_path)
    redis = client or FakeRedis()
    journals = JournalStore(vfs)
    return redis, journals, RedisSessionContext(
        redis,
        journals,
        **context_kwargs,
    )


def test_context_returns_summary_then_recent_n_messages(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    context.append_message("user-1", "sess-1", {"role": "user", "content": "one"})
    context.append_message(
        "user-1", "sess-1", {"role": "assistant", "content": "two"}
    )
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "three"}
    )
    context.set_summary("user-1", "sess-1", "earlier summary")

    assert context.build_history("user-1", "sess-1", 2) == (
        {"role": "system", "content": "earlier summary"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    )


def test_context_builds_semantic_then_compressed_then_recent_history(
    tmp_path: Path,
) -> None:
    _, _, context = _context(tmp_path, clock=lambda: NOW)
    document = {
        "user_id": "user-1",
        "session_id": "sess-1",
        "coverage": {"processed_message_count": 8},
        "current_goal": ["继续DREAM"],
        "preferences": [],
        "confirmed_facts": ["journals保存原文"],
        "pending_items": [],
        "attachment_references": [],
        "compression_context": {
            "messages": [
                {
                    "role": "tool",
                    "content": "older <<ccr:abc123>>",
                    "tool_call_id": "call_search",
                }
            ],
            "tokens_before": 100,
            "tokens_after": 30,
        },
        "updated_at": NOW.isoformat(),
    }
    context.set_summary(
        "user-1", "sess-1", json.dumps(document, ensure_ascii=False)
    )
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "recent"}
    )

    history = context.build_history("user-1", "sess-1", 10)
    semantic = json.loads(history[0]["content"])

    assert set(semantic) == {
        "current_goal",
        "preferences",
        "confirmed_facts",
        "pending_items",
        "attachment_references",
    }
    assert history[1] == {
        "role": "tool",
        "content": "older <<ccr:abc123>>",
        "tool_call_id": "call_search",
    }
    assert history[2] == {"role": "user", "content": "recent"}
    assert "user-1" not in history[0]["content"]
    assert "sess-1" not in history[0]["content"]


def test_context_attachment_is_recorded_without_message_content(
    tmp_path: Path,
) -> None:
    telemetry = InMemoryHeadroomTelemetry()
    _, _, context = _context(
        tmp_path,
        telemetry=telemetry,
        clock=lambda: NOW,
    )
    document = {
        "user_id": "user-1",
        "session_id": "sess-1",
        "coverage": {"processed_message_count": 8},
        "current_goal": [],
        "preferences": [],
        "confirmed_facts": [],
        "pending_items": [],
        "attachment_references": [],
        "compression_context": {
            "messages": [{"role": "assistant", "content": "private"}],
            "tokens_before": 100,
            "tokens_after": 30,
        },
        "updated_at": NOW.isoformat(),
    }
    context.set_summary("user-1", "sess-1", json.dumps(document))

    context.build_history("user-1", "sess-1", 10)

    assert telemetry.snapshot().context_attached_count == 1


def test_every_write_sets_half_day_ttl(tmp_path: Path) -> None:
    redis, _, context = _context(tmp_path)

    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "hello"}
    )
    context.set_summary("user-1", "sess-1", "summary")

    assert len(redis.expirations) == 2
    assert {seconds for _, seconds in redis.expirations} == {43_200}


def test_message_append_uses_transaction_and_refreshes_both_ttls(
    tmp_path: Path,
) -> None:
    redis, _, context = _context(tmp_path)
    context.set_summary("user-1", "sess-1", "summary")

    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "hello"}
    )

    assert redis.pipeline_transactions[-1] is True
    assert len(redis.ttls) == 2
    assert set(redis.ttls.values()) == {43_200}


def test_recent_turns_excludes_orphaned_older_assistant(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    for role, content in (
        ("user", "u1"),
        ("assistant", "a1"),
        ("user", "u2"),
        ("assistant", "a2"),
        ("user", "u3"),
    ):
        context.append_message(
            "user-1", "sess-1", {"role": role, "content": content}
        )

    assert context.recent_turns("user-1", "sess-1", 2) == (
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    )


def test_session_exists_when_summary_exists(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    assert context.has_session("user-1", "sess-1") is False

    context.set_summary("user-1", "sess-1", "summary")

    assert context.has_session("user-1", "sess-1") is True


def test_expired_context_restores_matching_session_from_journals(
    tmp_path: Path,
) -> None:
    redis, journals, context = _context(tmp_path)
    journals.append_message(
        "user-1", "sess-1", role="user", content="restore me", timestamp=NOW
    )
    journals.append_message(
        "user-1", "other", role="user", content="exclude me", timestamp=NOW
    )

    context.ensure_session_loaded("user-1", "sess-1", 10)
    history = context.build_history("user-1", "sess-1", 10)

    assert history == ({"role": "user", "content": "restore me"},)
    assert len(redis.lists) == 1


class FixedSnapshotReader:
    def __init__(self, summary: str | None) -> None:
        self.summary = summary
        self.calls: list[tuple[str, str]] = []

    def read(self, user_id: str, session_id: str) -> str | None:
        self.calls.append((user_id, session_id))
        return self.summary


class RecordingRecoveryQueue:
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


def _append_message_pairs(
    journals: JournalStore,
    *,
    pair_count: int,
) -> None:
    for index in range(pair_count):
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


def summary_document_json(*, updated_at: str, compressed_content: str) -> str:
    return json.dumps(
        {
            "user_id": "user-1",
            "session_id": "sess-1",
            "coverage": {"processed_message_count": 4},
            "current_goal": ["continue DREAM"],
            "preferences": [],
            "confirmed_facts": ["journals preserve originals"],
            "pending_items": [],
            "attachment_references": [],
            "compression_context": {
                "messages": [
                    {"role": "assistant", "content": compressed_content}
                ],
                "tokens_before": 100,
                "tokens_after": 30,
            },
            "updated_at": updated_at,
        },
        ensure_ascii=False,
    )


def test_expired_snapshot_uses_semantic_summary_and_queues_older_journals(
    tmp_path: Path,
) -> None:
    snapshot = summary_document_json(
        updated_at="2026-07-31T00:00:00+00:00",
        compressed_content="stale <<ccr:expired>>",
    )
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=FixedSnapshotReader(snapshot),
        recovery_queue=recovery_queue,
        ccr_ttl_seconds=3_600,
        clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _append_message_pairs(journals, pair_count=2)

    context.ensure_session_loaded("user-1", "sess-1", turns=1)
    history = context.build_history("user-1", "sess-1", turns=1)

    assert "stale" not in json.dumps(history, ensure_ascii=False)
    assert "current_goal" in history[0]["content"]
    assert recovery_queue.calls[0][2] == (
        {"role": "user", "content": "question-0"},
        {"role": "assistant", "content": "answer-0"},
    )


def test_fresh_snapshot_keeps_compressed_context_without_rebuild(
    tmp_path: Path,
) -> None:
    snapshot = summary_document_json(
        updated_at="2026-08-01T00:30:00+00:00",
        compressed_content="fresh <<ccr:available>>",
    )
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=FixedSnapshotReader(snapshot),
        recovery_queue=recovery_queue,
        ccr_ttl_seconds=3_600,
        clock=lambda: datetime(2026, 8, 1, 1, tzinfo=timezone.utc),
    )
    _append_message_pairs(journals, pair_count=2)

    context.ensure_session_loaded("user-1", "sess-1", turns=1)
    history = context.build_history("user-1", "sess-1", turns=1)

    assert "fresh <<ccr:available>>" in json.dumps(
        history, ensure_ascii=False
    )
    assert recovery_queue.calls == []


def test_expired_session_prefers_existing_summary_snapshot(
    tmp_path: Path,
) -> None:
    snapshot_reader = FixedSnapshotReader("existing snapshot")
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=snapshot_reader,
        recovery_queue=recovery_queue,
    )
    _append_message_pairs(journals, pair_count=2)

    context.ensure_session_loaded("user-1", "sess-1", turns=1)

    assert snapshot_reader.calls == [("user-1", "sess-1")]
    assert context.get_summary("user-1", "sess-1") == "existing snapshot"
    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "question-1"},
        {"role": "assistant", "content": "answer-1"},
    )
    assert recovery_queue.calls == []


def test_expired_session_without_snapshot_restores_recent_turns_and_queues_rebuild(
    tmp_path: Path,
) -> None:
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=FixedSnapshotReader(None),
        recovery_queue=recovery_queue,
    )
    _append_message_pairs(journals, pair_count=3)

    context.ensure_session_loaded("user-1", "sess-1", turns=1)

    assert context.get_summary("user-1", "sess-1") is None
    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "question-2"},
        {"role": "assistant", "content": "answer-2"},
    )
    assert recovery_queue.calls == [
        (
            "user-1",
            "sess-1",
            (
                {"role": "user", "content": "question-0"},
                {"role": "assistant", "content": "answer-0"},
                {"role": "user", "content": "question-1"},
                {"role": "assistant", "content": "answer-1"},
            ),
            0,
            1,
        )
    ]


def test_restoration_keeps_attachment_markers_without_counting_them_as_turns(
    tmp_path: Path,
) -> None:
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=FixedSnapshotReader(None),
        recovery_queue=recovery_queue,
    )
    journals.append_message(
        "user-1", "sess-1", role="user", content="old question", timestamp=NOW
    )
    journals.append_file(
        "user-1",
        "sess-1",
        original_url="https://example.test/old.pdf",
        local_path="raw/old.pdf",
        timestamp=NOW,
    )
    journals.append_message(
        "user-1", "sess-1", role="assistant", content="old answer", timestamp=NOW
    )
    journals.append_message(
        "user-1", "sess-1", role="user", content="recent question", timestamp=NOW
    )
    journals.append_file(
        "user-1",
        "sess-1",
        original_url="https://example.test/recent.pdf",
        local_path="raw/recent.pdf",
        timestamp=NOW,
    )
    journals.append_message(
        "user-1",
        "sess-1",
        role="assistant",
        content="recent answer",
        timestamp=NOW,
    )

    context.ensure_session_loaded("user-1", "sess-1", turns=1)

    recent = context.recent_messages("user-1", "sess-1", 10)
    assert tuple(message["role"] for message in recent) == (
        "user",
        "system",
        "assistant",
    )
    assert recent[1]["content"] == "[attachment: raw/recent.pdf]"
    recovery_messages = recovery_queue.calls[0][2]
    assert {message["content"] for message in recovery_messages} == {
        "old question",
        "[attachment: raw/old.pdf]",
        "old answer",
    }


def test_recovery_summary_preserves_all_current_redis_messages(
    tmp_path: Path,
) -> None:
    recovery_queue = RecordingRecoveryQueue()
    _, journals, context = _context(
        tmp_path,
        snapshot_reader=FixedSnapshotReader(None),
        recovery_queue=recovery_queue,
    )
    _append_message_pairs(journals, pair_count=2)

    context.ensure_session_loaded("user-1", "sess-1", turns=1)
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "new message"}
    )
    processed_message_count = recovery_queue.calls[0][3]
    context.store_compression_result(
        "user-1",
        "sess-1",
        "rebuilt summary",
        processed_message_count,
        1,
    )

    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "question-1"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "new message"},
    )


def test_headroom_triggers_for_token_ratio_message_count_or_duration() -> None:
    policy = HeadroomPolicy(
        context_window_tokens=100,
        trigger_ratio=0.65,
        max_messages=10,
        max_session_seconds=3_600,
    )

    assert policy.should_compress(
        estimated_tokens=65, message_count=1, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=10, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=1, session_seconds=3_600
    )
    assert not policy.should_compress(
        estimated_tokens=64, message_count=9, session_seconds=3_599
    )


def test_headroom_summary_is_stored_only_in_redis(tmp_path: Path) -> None:
    redis, journals, context = _context(tmp_path)
    journals.append_message(
        "user-1", "sess-1", role="user", content="hello", timestamp=NOW
    )
    before = journals.read_session("user-1", "sess-1")

    context.set_summary("user-1", "sess-1", "compressed conversation")

    assert context.get_summary("user-1", "sess-1") == "compressed conversation"
    assert journals.read_session("user-1", "sess-1") == before
    assert "compressed conversation" in redis.values.values()


def test_build_history_does_not_read_journals_without_explicit_restore(
    tmp_path: Path,
) -> None:
    _, journals, context = _context(tmp_path)
    journals.append_message(
        "user-1", "sess-1", role="user", content="journal only", timestamp=NOW
    )

    assert context.build_history("user-1", "sess-1", 10) == ()


def test_compression_snapshot_counts_only_redis_messages(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    context.set_summary("user-1", "sess-1", "summary")
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "one"}
    )
    context.append_message(
        "user-1", "sess-1", {"role": "assistant", "content": "two"}
    )

    snapshot = context.compression_snapshot("user-1", "sess-1")

    assert snapshot.processed_message_count == 2
    assert snapshot.messages == (
        {"role": "system", "content": "summary"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    )


def test_headroom_job_can_trim_redis_to_recent_messages(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    for content in ("one", "two", "three"):
        context.append_message(
            "user-1",
            "sess-1",
            {"role": "user", "content": content},
        )

    context.trim_messages("user-1", "sess-1", 2)

    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "two"},
        {"role": "user", "content": "three"},
    )


def test_compression_result_preserves_messages_appended_after_snapshot(
    tmp_path: Path,
) -> None:
    _, _, context = _context(tmp_path)
    for index in range(4):
        context.append_message(
            "user-1",
            "sess-1",
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)},
        )
    snapshot = context.compression_snapshot("user-1", "sess-1")
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "concurrent"}
    )

    context.store_compression_result(
        "user-1",
        "sess-1",
        "summary",
        processed_message_count=snapshot.processed_message_count,
        keep_recent_turns=1,
    )

    assert context.get_summary("user-1", "sess-1") == "summary"
    assert context.recent_messages("user-1", "sess-1", 10) == (
        {"role": "user", "content": "2"},
        {"role": "assistant", "content": "3"},
        {"role": "user", "content": "concurrent"},
    )


def test_delete_removes_only_target_session_context(tmp_path: Path) -> None:
    _, _, context = _context(tmp_path)
    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "target"}
    )
    context.append_message(
        "user-2", "sess-1", {"role": "user", "content": "other"}
    )
    context.set_summary("user-1", "sess-1", "target summary")

    context.delete("user-1", "sess-1")

    assert context.build_history("user-1", "sess-1", 10) == ()
    assert context.build_history("user-2", "sess-1", 10) == (
        {"role": "user", "content": "other"},
    )


def test_headroom_rejects_trigger_ratio_outside_plan_range() -> None:
    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        HeadroomPolicy(
            context_window_tokens=100,
            trigger_ratio=0.75,
            max_messages=10,
            max_session_seconds=3_600,
        )
