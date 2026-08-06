import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.factories import memory_event
from short_term_memory.models import JournalRole
from short_term_memory.storage.journal_store import (
    JournalConflictError,
    JournalFileEvent,
    JournalMessageEvent,
    JournalStore,
)
from short_term_memory.storage.vfs_adapter import VFSAdapter


NOW = datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc)


def test_message_row_uses_plan_fields_and_session_filename(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    path = store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content="帮我总结这个 PDF",
        timestamp=NOW,
    )

    assert path.name == "2026-07-23-sess-1.jsonl"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "type": "message",
        "role": "user",
        "content": "帮我总结这个 PDF",
        "timestamp": "2026-07-23T06:30:00+00:00",
    }


def test_multimodal_content_keeps_only_input_text(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content=[
            {"type": "input_text", "text": "第一段"},
            {"type": "input_image", "image_url": "image.png"},
            {"type": "input_text", "text": "第二段"},
        ],
        timestamp=NOW,
    )

    event = store.read_session("user-1", "sess-1")[0]
    assert isinstance(event, JournalMessageEvent)
    assert event.content == "第一段\n第二段"


def test_file_row_falls_back_to_original_url(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_file(
        "user-1",
        "sess-1",
        original_url="https://example.com/product-plan.pdf",
        local_path=None,
        timestamp=NOW,
    )

    event = store.read_session("user-1", "sess-1")[0]
    assert isinstance(event, JournalFileEvent)
    assert event.local_path == "https://example.com/product-plan.pdf"


def test_session_read_sorts_daily_files_and_does_not_persist_scope_ids(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content="第二天",
        timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    store.append_message(
        "user-1",
        "sess-1",
        role="assistant",
        content="第一天",
        timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    events = store.read_session("user-1", "sess-1")
    assert [event.content for event in events if isinstance(event, JournalMessageEvent)] == [
        "第一天",
        "第二天",
    ]
    for path in VFSAdapter(tmp_path).paths("user-1").journals.glob("*.jsonl"):
        assert "user_id" not in path.read_text(encoding="utf-8")
        assert "session_id" not in path.read_text(encoding="utf-8")


def test_append_event_is_byte_preserving_and_idempotent(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(sequence=7, event_id="same", content="a\n中文\n")

    first = store.append_event("u", "s", event)
    second = store.append_event("u", "s", event)

    assert first.appended is True
    assert second.appended is False
    assert store.find_event("u", "s", "same") == event
    assert store.read_original_range("u", "s", 7, 7)[0].content == "a\n中文\n"
    assert json.loads(first.path.read_text(encoding="utf-8")) == {
        "type": "message",
        "role": "user",
        "content": "a\n中文\n",
        "timestamp": event.created_at,
        "event_id": "same",
        "sequence": 7,
        "content_type": "conversation",
        "metadata": {},
        "sha256": event.sha256,
    }


def test_same_event_id_with_different_digest_is_conflict(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(event_id="same", content="one"))

    with pytest.raises(JournalConflictError):
        store.append_event("u", "s", memory_event(event_id="same", content="two"))


def test_read_original_range_selects_only_requested_sequences(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    for sequence in (1, 2, 3):
        store.append_event(
            "u",
            "s",
            memory_event(sequence=sequence, event_id=f"event-{sequence}"),
        )

    assert store.read_original_range("u", "s", 2, 2) == (
        memory_event(sequence=2, event_id="event-2"),
    )


def test_read_recent_originals_uses_turns_and_never_starts_with_assistant(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    events = (
        memory_event(sequence=1, event_id="one", content="one"),
        memory_event(sequence=2, event_id="two", content="two").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
        memory_event(sequence=3, event_id="three", content="three"),
    )
    for event in events:
        store.append_event("u", "s", event)

    assert store.read_recent_originals("u", "s", 1) == events[2:]


def test_recent_originals_are_sorted_by_sequence_after_out_of_order_appends(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    later = memory_event(sequence=2, event_id="later", content="later")
    first = memory_event(sequence=1, event_id="first", content="first")
    store.append_event("u", "s", later)
    store.append_event("u", "s", first)

    assert store.read_recent_originals("u", "s", 2) == (first, later)


def test_incomplete_final_json_line_is_ignored_as_crash_residue(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(event_id="durable")
    result = store.append_event("u", "s", event)
    with result.path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message"')

    assert store.find_event("u", "s", "durable") == event
    assert store.read_original_range("u", "s", 1, 1) == (event,)


def test_session_read_rejects_whitespace_middle_corruption(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    first = store.append_event("u", "s", memory_event(sequence=1, event_id="one"))
    store.append_event("u", "s", memory_event(sequence=2, event_id="two"))
    durable_line, valid_line = first.path.read_text(encoding="utf-8").splitlines()
    first.path.write_text(
        f"{durable_line}\n \t\n{valid_line}\n",
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        store.read_session("u", "s")


def test_sequence_event_preserves_original_offset_timestamp(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(
        event_id="offset",
        created_at=datetime(2026, 8, 6, 8, tzinfo=timezone(timedelta(hours=8))),
    )

    result = store.append_event("u", "s", event)

    assert json.loads(result.path.read_text(encoding="utf-8"))["timestamp"] == event.created_at
    assert store.find_event("u", "s", "offset") == event
    assert store.read_original_range("u", "s", 1, 1) == (event,)
