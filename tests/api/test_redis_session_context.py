from datetime import datetime, timezone
from pathlib import Path

import pytest

from dream.api.conversation_handler import HeadroomPolicy, RedisSessionContext
from dream.storage.journal_store import JournalStore
from dream.storage.vfs_adapter import VFSAdapter
from dream.storage.wiki_store import WikiStore


NOW = datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc)


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}
        self.expirations: list[tuple[str, int]] = []

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(key, [])
        if start < 0:
            start = max(0, len(values) + start)
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def expire(self, key: str, seconds: int) -> None:
        self.expirations.append((key, seconds))

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.lists.pop(key, None)
            self.values.pop(key, None)


def _context(tmp_path: Path, client: FakeRedis | None = None):
    vfs = VFSAdapter(tmp_path)
    redis = client or FakeRedis()
    journals = JournalStore(vfs)
    return redis, journals, RedisSessionContext(redis, journals)


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
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    )


def test_every_write_sets_half_day_ttl(tmp_path: Path) -> None:
    redis, _, context = _context(tmp_path)

    context.append_message(
        "user-1", "sess-1", {"role": "user", "content": "hello"}
    )
    context.set_summary("user-1", "sess-1", "summary")

    assert len(redis.expirations) == 2
    assert {seconds for _, seconds in redis.expirations} == {43_200}


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

    history = context.build_history("user-1", "sess-1", 10)

    assert history == ({"role": "user", "content": "restore me"},)
    assert len(redis.lists) == 1


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
    assert WikiStore(journals.vfs).list_files("user-1") == ()
    assert "compressed conversation" in redis.values.values()


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
