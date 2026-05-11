import json
import pytest
from unittest.mock import AsyncMock
from memory_system.core.session_manager import SessionManager


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.fixture
def session_mgr(settings):
    return SessionManager(settings)


@pytest.mark.asyncio
async def test_get_session_empty(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {}

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert result == {"rounds": []}


@pytest.mark.asyncio
async def test_get_session_existing(session_mgr, redis_mock):
    redis_mock.hgetall.return_value = {
        "rounds": '[{"round_id": "r1", "messages": [{"role": "user", "content": "hi"}], "first_user_text": "hi"}]',
    }

    result = await session_mgr.get_session(redis_mock, "u1", "s1")
    assert len(result["rounds"]) == 1
    assert result["rounds"][0]["messages"][0]["content"] == "hi"


@pytest.mark.asyncio
async def test_add_round_single_block(session_mgr, redis_mock):
    """All input messages stored as one block. No embedding calls."""
    redis_mock.hgetall.return_value = {"rounds": "[]"}

    new_msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    call_args = redis_mock.hset.call_args
    mapping = call_args[1]["mapping"]
    rounds = json.loads(mapping["rounds"])

    assert len(rounds) == 1
    assert len(rounds[0]["messages"]) == 4
    assert rounds[0]["messages"][0]["content"] == "q1"
    assert rounds[0]["messages"][3]["content"] == "a2"
    assert rounds[0]["first_user_text"] == "q1"


@pytest.mark.asyncio
async def test_add_round_first_user_text(settings, redis_mock):
    """first_user_text comes from first user message, not later ones."""
    mgr = SessionManager(settings)
    redis_mock.hgetall.return_value = {"rounds": "[]"}

    new_msgs = [
        {"role": "assistant", "content": "system intro"},
        {"role": "user", "content": "first real question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "another answer"},
    ]

    await mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    call_args = redis_mock.hset.call_args
    mapping = call_args[1]["mapping"]
    rounds = json.loads(mapping["rounds"])

    assert rounds[0]["first_user_text"] == "first real question"


@pytest.mark.asyncio
async def test_add_round_limits_total(session_mgr, redis_mock):
    """Old rounds trimmed when exceeding max."""
    import json

    existing = json.dumps([
        {"round_id": f"old{i}", "messages": [{"role": "user", "content": f"old{i}"}], "first_user_text": f"old{i}"}
        for i in range(1, 11)
    ])
    redis_mock.hgetall.return_value = {"rounds": existing}

    new_msgs = [
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new a"},
    ]

    await session_mgr.add_round(redis_mock, "u1", "s1", new_msgs)

    call_args = redis_mock.hset.call_args
    mapping = call_args[1]["mapping"]
    rounds = json.loads(mapping["rounds"])

    assert len(rounds) == 10
    assert rounds[0]["round_id"] == "old2"
    assert rounds[-1]["messages"][0]["content"] == "new"


@pytest.mark.asyncio
async def test_build_history(session_mgr, redis_mock):
    """Relevant older round shown fully, most recent round always full."""
    import json

    rounds = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "old question about Python"},
                {"role": "assistant", "content": "old answer about Python"},
            ],
            "first_user_text": "old question about Python",
        },
        {
            "round_id": "r2",
            "messages": [
                {"role": "user", "content": "recent q"},
                {"role": "assistant", "content": "recent a"},
            ],
            "first_user_text": "recent q",
        },
    ]
    redis_mock.hgetall.return_value = {"rounds": json.dumps(rounds)}

    query_text = "python questions"

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_text, recent_rounds_full=1,
    )

    assert "old question about Python" in result["history_str"]
    assert "old answer about Python" in result["history_str"]
    assert "recent a" in result["history_str"]
    assert len(result["history_messages"]) == 4


@pytest.mark.asyncio
async def test_build_history_irrelevant_archived(session_mgr, redis_mock):
    """Irrelevant older round: Q kept, A replaced with placeholder."""
    import json

    rounds = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "unrelated question about weather"},
                {"role": "assistant", "content": "unrelated answer about weather"},
            ],
            "first_user_text": "unrelated question about weather",
        },
        {
            "round_id": "r2",
            "messages": [
                {"role": "user", "content": "recent q"},
                {"role": "assistant", "content": "recent a"},
            ],
            "first_user_text": "recent q",
        },
    ]
    redis_mock.hgetall.return_value = {"rounds": json.dumps(rounds)}

    query_text = "python programming code"

    result = await session_mgr.build_history(
        redis_mock, "u1", "s1", query_text, recent_rounds_full=1,
    )

    assert "unrelated question about weather" in result["history_str"]
    assert "[previous response omitted]" in result["history_str"]
    assert "unrelated answer" not in result["history_str"]
    assert "recent a" in result["history_str"]


@pytest.mark.asyncio
async def test_build_history_empty(session_mgr, redis_mock):
    """Empty rounds returns empty history."""
    redis_mock.hgetall.return_value = {"rounds": "[]"}

    result = await session_mgr.build_history(redis_mock, "u1", "s1", "hello")

    assert result == {"history_messages": [], "history_str": ""}


@pytest.mark.asyncio
async def test_build_history_recent_rounds_full(session_mgr, redis_mock):
    """Last N rounds always full; older rounds filtered by bigram relevance."""
    import json

    rounds = [
        {
            "round_id": "r1",
            "messages": [
                {"role": "user", "content": "unrelated weather talk"},
                {"role": "assistant", "content": "weather answer"},
            ],
            "first_user_text": "unrelated weather talk",
        },
        {
            "round_id": "r2",
            "messages": [
                {"role": "user", "content": "also unrelated sports"},
                {"role": "assistant", "content": "sports answer"},
            ],
            "first_user_text": "also unrelated sports",
        },
        {
            "round_id": "r3",
            "messages": [
                {"role": "user", "content": "python question"},
                {"role": "assistant", "content": "python answer"},
            ],
            "first_user_text": "python question",
        },
        {
            "round_id": "r4",
            "messages": [
                {"role": "user", "content": "recent python"},
                {"role": "assistant", "content": "recent python answer"},
            ],
            "first_user_text": "recent python",
        },
        {
            "round_id": "r5",
            "messages": [
                {"role": "user", "content": "latest q"},
                {"role": "assistant", "content": "latest a"},
            ],
            "first_user_text": "latest q",
        },
    ]
    redis_mock.hgetall.return_value = {"rounds": json.dumps(rounds)}

    query_text = "python programming"

    # Default recent_rounds_full=3 → r3,r4,r5 always full; r1,r2 go through bigram
    result = await session_mgr.build_history(redis_mock, "u1", "s1", query_text)

    # r1 irrelevant: Q kept, A replaced with placeholder
    assert "unrelated weather talk" in result["history_str"]
    # r2 irrelevant: Q kept, A replaced
    assert "also unrelated sports" in result["history_str"]
    # r3,r4,r5 always full
    assert "python answer" in result["history_str"]
    assert "recent python answer" in result["history_str"]
    assert "latest a" in result["history_str"]
    assert "[previous response omitted]" in result["history_str"]

    # r1+r2 Q-only = 2 user msgs, r2 irrelevant = 2 placeholders
    # = 4 msgs for old. r3,r4,r5 = 6 msgs → total 10
    assert len(result["history_messages"]) == 10


def test_bigrams():
    assert SessionManager._bigrams("hello") == {"he", "el", "ll", "lo"}


def test_bigram_jaccard_identical():
    assert SessionManager._bigram_jaccard("hello world", "hello world") == 1.0


def test_bigram_jaccard_different():
    assert SessionManager._bigram_jaccard("hello", "xyz") < 0.1
