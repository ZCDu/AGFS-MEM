import json
from datetime import datetime, timezone
from pathlib import Path

from short_term_memory.storage.journal_store import (
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
