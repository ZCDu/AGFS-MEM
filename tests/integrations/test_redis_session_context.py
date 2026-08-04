import os
import time
from uuid import uuid4

import pytest

from dream.api.conversation_handler import RedisSessionContext
from dream.api.redis_runtime import RedisRuntime
from dream.storage.journal_store import JournalStore
from dream.storage.vfs_adapter import VFSAdapter


pytestmark = pytest.mark.skipif(
    os.environ.get("DREAM_RUN_REDIS_INTEGRATION") != "1",
    reason="set DREAM_RUN_REDIS_INTEGRATION=1 to run Redis 7.2 integration",
)


@pytest.fixture
def runtime():
    value = RedisRuntime.connect(
        os.environ.get("DREAM_REDIS_URL", "redis://127.0.0.1:6379/0")
    )
    try:
        yield value
    finally:
        value.close()


def _context(runtime, tmp_path, *, ttl_seconds: int = 43_200):
    journals = JournalStore(VFSAdapter(tmp_path))
    return journals, RedisSessionContext(
        runtime.client, journals, ttl_seconds=ttl_seconds
    )


def test_real_redis_orders_messages_and_expires_both_keys(runtime, tmp_path) -> None:
    user_id = f"redis-test-{uuid4().hex}"
    journals, context = _context(runtime, tmp_path, ttl_seconds=1)
    try:
        context.append_message(user_id, "session", {"role": "user", "content": "one"})
        context.set_summary(user_id, "session", "summary")
        context.append_message(
            user_id, "session", {"role": "assistant", "content": "two"}
        )
        assert context.build_history(user_id, "session", 10) == (
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        )
        time.sleep(1.1)
        assert context.has_session(user_id, "session") is False
    finally:
        context.delete(user_id, "session")


def test_real_redis_preserves_concurrent_message_during_trim(runtime, tmp_path) -> None:
    user_id = f"redis-test-{uuid4().hex}"
    _, context = _context(runtime, tmp_path)
    try:
        for index in range(4):
            context.append_message(
                user_id,
                "session",
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": str(index),
                },
            )
        snapshot = context.compression_snapshot(user_id, "session")
        context.append_message(
            user_id, "session", {"role": "user", "content": "concurrent"}
        )
        context.store_compression_result(
            user_id,
            "session",
            "summary",
            snapshot.processed_message_count,
            1,
        )
        assert context.recent_messages(user_id, "session", 10)[-1]["content"] == "concurrent"
    finally:
        context.delete(user_id, "session")


def test_real_redis_expiry_restores_matching_journal_session(runtime, tmp_path) -> None:
    user_id = f"redis-test-{uuid4().hex}"
    journals, context = _context(runtime, tmp_path, ttl_seconds=1)
    journals.append_message(
        user_id, "session", role="user", content="restore me"
    )
    try:
        context.append_message(
            user_id, "session", {"role": "user", "content": "restore me"}
        )
        time.sleep(1.1)
        context.ensure_session_loaded(user_id, "session", 10)
        assert context.build_history(user_id, "session", 10) == (
            {"role": "user", "content": "restore me"},
        )
    finally:
        context.delete(user_id, "session")
