import json
import pytest
from unittest.mock import AsyncMock, patch
from memory_system.core.session_manager import SessionManager


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.fixture
def emb_client_mock():
    mock = AsyncMock()
    mock.get_embedding.return_value = [0.1, 0.2, 0.3]
    return mock


@pytest.fixture
def session_mgr(settings, emb_client_mock):
    return SessionManager(settings, emb_client_mock)


@pytest.mark.asyncio
async def test_get_session_empty(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {}

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert result == {"messages": [], "archived_rounds": []}


@pytest.mark.asyncio
async def test_get_session_existing(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {
        "messages": json.dumps([{"role": "user", "content": "hi"}]),
        "archived_rounds": json.dumps([]),
        "created_at": "2026-05-04T10:00:00Z",
    }

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_add_round_within_window(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {
        "messages": json.dumps([{"role": "user", "content": "old"}]),
        "archived_rounds": json.dumps([]),
    }

    new_msgs = [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    # Verify HSET was called
    call_args = redis_mock.hset.call_args
    assert call_args is not None
    key = call_args[0][0]
    assert "u1" in key and "s1" in key


@pytest.mark.asyncio
async def test_add_round_exceeds_window(session_mgr, redis_mock):
    # 3 messages in window (3 rounds), adding another should archive oldest
    existing = json.dumps([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ])
    redis_mock.hgetall.return_value = {
        "messages": existing,
        "archived_rounds": json.dumps([]),
    }

    new_msgs = [
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    # Should have archived q1,a1
    call_args = redis_mock.hset.call_args
    mapping = call_args[1]["mapping"]
    messages = json.loads(mapping["messages"])
    archived = json.loads(mapping["archived_rounds"])

    # After adding q4,a4, should still have 6 messages (3 rounds * 2)
    assert len(messages) == 6
    assert messages[0]["content"] == "q2"  # q1 was archived
    assert len(archived) == 1  # one archived round


@pytest.mark.asyncio
async def test_build_history(session_mgr, redis_mock):
    archived = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            "embedding": [0.1, 0.2, 0.3],
        }
    ]
    messages = [
        {"role": "user", "content": "recent q"},
        {"role": "assistant", "content": "recent a"},
    ]
    redis_mock.hgetall.return_value = {
        "messages": json.dumps(messages),
        "archived_rounds": json.dumps(archived),
    }

    query_embedding = [0.1, 0.2, 0.3]  # high similarity

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_embedding
    )

    # archived round should be included because of high relevance
    assert "old question" in result["history_str"]
    assert "old answer" in result["history_str"]
    assert len(result["history_messages"]) == 4  # 2 archived + 2 recent


@pytest.mark.asyncio
async def test_build_history_irrelevant_archived(session_mgr, redis_mock):
    archived = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "unrelated question"},
                {"role": "assistant", "content": "unrelated answer"},
            ],
            "embedding": [0.9, -0.8, -0.7],
        }
    ]
    messages = [{"role": "user", "content": "recent q"}]
    redis_mock.hgetall.return_value = {
        "messages": json.dumps(messages),
        "archived_rounds": json.dumps(archived),
    }

    query_embedding = [0.1, 0.2, 0.3]  # low similarity (cos sim ~= -0.54 < 0.7)

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_embedding
    )

    # unrelated question should be truncated, answer omitted
    assert "unrelated question" in result["history_str"]
    assert "[previous response omitted]" in result["history_str"]
    assert "unrelated answer" not in result["history_str"]
