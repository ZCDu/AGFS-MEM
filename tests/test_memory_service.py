import pytest
from unittest.mock import AsyncMock, patch
from memory_system.core.memory_service import MemoryService


@pytest.fixture
def services(settings):
    session_mgr = AsyncMock()
    long_term = AsyncMock()
    extractor = AsyncMock()
    embedding_client = AsyncMock()
    embedding_client.get_embedding.return_value = [0.1, 0.2, 0.3]

    session_mgr.get_session.return_value = {
        "messages": [{"role": "user", "content": "hi"}],
        "archived_rounds": [],
    }
    session_mgr.build_history.return_value = {
        "history_messages": [{"role": "user", "content": "hi"}],
        "history_str": "user: hi",
    }
    long_term.search.return_value = []
    extractor.extract_memories.return_value = []

    return session_mgr, long_term, extractor, embedding_client


@pytest.mark.asyncio
async def test_process_minimal_request(settings, services):
    session_mgr, long_term, extractor, emb_client = services
    svc = MemoryService(
        settings, session_mgr, long_term, extractor, emb_client,
        redis_client=AsyncMock(), es_client=AsyncMock(),
    )
    mock_redis = AsyncMock()
    mock_es = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis
    svc._es_client.get_es.return_value = mock_es

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )

    mock_redis.hgetall.return_value = {}
    mock_redis.hset = AsyncMock()

    resp = await svc.process(req)

    assert resp.object == "memory.response"
    assert resp.model == "memory-v1"


@pytest.mark.asyncio
async def test_process_with_retrieved_memories(settings, services):
    session_mgr, long_term, extractor, emb_client = services
    long_term.search.return_value = [
        {
            "_id": "mem_1",
            "memory": "user likes Python",
            "score": 0.95,
            "time_decayed_score": 0.95,
            "created_at": "2026-05-01T10:00:00Z",
            "importance": 0.8,
        }
    ]

    svc = MemoryService(
        settings, session_mgr, long_term, extractor, emb_client,
        redis_client=AsyncMock(), es_client=AsyncMock(),
    )
    mock_redis = AsyncMock()
    mock_es = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis
    svc._es_client.get_es.return_value = mock_es

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="what's my favorite language?")],
    )

    mock_redis.hgetall.return_value = {
        "messages": '[]',
        "archived_rounds": '[]',
    }
    mock_redis.hset = AsyncMock()

    resp = await svc.process(req)

    assert len(resp.retrieved_memories) == 1
    assert resp.retrieved_memories[0].memory == "user likes Python"
