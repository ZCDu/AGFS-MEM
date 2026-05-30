import asyncio
import pytest
from unittest.mock import AsyncMock
from memory_system.api.models import Usage
from memory_system.core.memory_service import MemoryService


@pytest.fixture
def services(settings):
    session_mgr = AsyncMock()
    extractor = AsyncMock()
    embedding_client = AsyncMock()
    es_client = AsyncMock()
    redis_client = AsyncMock()
    local_storage = None

    embedding_client.get_embedding.return_value = ([0.1, 0.2, 0.3], {})

    session_mgr.get_session.return_value = {
        "rounds": [{
            "round_id": "r1",
            "messages": [{"role": "user", "content": "hi"}],
            "first_user_text": "hi",
        }],
    }
    session_mgr.build_history.return_value = {
        "history_messages": [{"role": "user", "content": "hi"}],
        "history_str": "user: hi",
    }
    es_client.search_knn.return_value = []
    extractor.extract.return_value = ([], Usage())
    extractor.update_memory.return_value = ([], Usage())

    return {
        "session_mgr": session_mgr,
        "extractor": extractor,
        "embedding_client": embedding_client,
        "es_client": es_client,
        "redis_client": redis_client,
        "local_storage": local_storage,
    }


@pytest.mark.asyncio
async def test_store_minimal_request(settings, services):
    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    mock_redis = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )

    mock_redis.hgetall.return_value = {}
    mock_redis.hset = AsyncMock()

    resp = await svc.store(req)

    assert resp.model == "memory-v1"
    assert resp.status == "stored"

    await asyncio.sleep(0)
    services["extractor"].extract.assert_called_once()


@pytest.mark.asyncio
async def test_recall_minimal_request(settings, services):
    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    mock_redis = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="hello")],
    )

    mock_redis.hgetall.return_value = {}
    mock_redis.hset = AsyncMock()

    resp = await svc.recall(req)

    assert resp.model == "memory-v1"
    assert len(resp.history) == 1


@pytest.mark.asyncio
async def test_recall_with_retrieved_memories(settings, services):
    services["es_client"].search_knn.return_value = [
        {
            "id": "mem_1",
            "memory": "user likes Python",
            "score": 0.95,
            "user_id": "u1",
            "created_at": "2026-05-01T10:00:00Z",
        }
    ]

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    mock_redis = AsyncMock()
    svc._redis_client.get_redis.return_value = mock_redis

    from memory_system.api.models import MemoryRequest, Message

    req = MemoryRequest(
        userId="u1",
        sessionId="s1",
        input=[Message(role="user", content="what's my favorite language?")],
    )

    mock_redis.hgetall.return_value = {}
    mock_redis.hset = AsyncMock()

    resp = await svc.recall(req)

    assert len(resp.retrieved_memories) == 1
    assert resp.retrieved_memories[0].memory == "user likes Python"
    assert resp.history[0].role == "system"
    assert "Untrusted retrieved user memories" in resp.history[0].content
    assert "user likes Python" in resp.history[0].content


@pytest.mark.asyncio
async def test_recall_filters_long_term_memory_with_dedicated_score_threshold(settings, services):
    settings.memory_score_threshold = 1.1
    services["es_client"].search_knn.return_value = [
        {
            "id": "mem_1",
            "memory": "weakly related memory",
            "score": 0.95,
            "user_id": "u1",
            "created_at": "2026-05-01T10:00:00Z",
        }
    ]

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )
    svc._redis_client.get_redis.return_value = AsyncMock()

    from memory_system.api.models import MemoryRequest, Message

    resp = await svc.recall(
        MemoryRequest(
            userId="u1",
            sessionId="s1",
            input=[Message(role="user", content="what do I like?")],
        )
    )

    assert resp.retrieved_memories == []
    assert all("weakly related memory" not in m.content for m in resp.history)


@pytest.mark.asyncio
async def test_extract_and_store_adds_new_facts(settings, services):
    """Facts extracted with no similar memories → direct ADD."""
    services["extractor"].extract.return_value = (
        ["User's name is John", "User works at Google"],
        Usage(input_tokens=50, output_tokens=30, total_tokens=80),
    )
    services["es_client"].search_knn.return_value = []  # no similar memories

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    # Execute _extract_and_store synchronously to test it
    await svc._extract_and_store("u1", [
        {"role": "user", "content": "I'm John and I work at Google"},
    ])

    assert services["es_client"].index_document.call_count == 2


@pytest.mark.asyncio
async def test_extract_and_store_scopes_generated_doc_ids_by_user(settings, services):
    """Same fact for different users should not overwrite the same ES document."""
    services["extractor"].extract.return_value = (
        ["Likes Python"],
        Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    )
    services["es_client"].search_knn.return_value = []

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    await svc._extract_and_store("u1", [{"role": "user", "content": "I like Python"}])
    await svc._extract_and_store("u2", [{"role": "user", "content": "I like Python"}])

    calls = services["es_client"].index_document.call_args_list
    assert calls[0].kwargs["doc_id"] != calls[1].kwargs["doc_id"]


@pytest.mark.asyncio
async def test_extract_and_store_updates_existing(settings, services):
    """Similar memories exist → LLM decides UPDATE."""
    services["extractor"].extract.return_value = (
        ["Lives in Beijing now"],
        Usage(input_tokens=30, output_tokens=15, total_tokens=45),
    )
    services["es_client"].search_knn.return_value = [
        {"id": "abc123", "memory": "Lives in Shanghai", "score": 0.9, "user_id": "u1", "created_at": "2026-01-01T00:00:00Z"},
    ]
    services["extractor"].update_memory.return_value = (
        [
            {"id": "abc123", "text": "Lives in Beijing now", "event": "UPDATE", "old_memory": "Lives in Shanghai"},
        ],
        Usage(input_tokens=60, output_tokens=40, total_tokens=100),
    )

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    await svc._extract_and_store("u1", [
        {"role": "user", "content": "I moved from Shanghai to Beijing"},
    ])

    services["es_client"].update_document.assert_called_once()
    call_args = services["es_client"].update_document.call_args
    assert call_args[1]["doc_id"] == "abc123"
    assert call_args[1]["data"] == "Lives in Beijing now"


@pytest.mark.asyncio
async def test_extract_and_store_delete(settings, services):
    """Contradicting fact → LLM decides DELETE."""
    services["extractor"].extract.return_value = (
        ["Dislikes coffee"],
        Usage(input_tokens=20, output_tokens=10, total_tokens=30),
    )
    services["es_client"].search_knn.return_value = [
        {"id": "mem_1", "memory": "Likes coffee", "score": 0.9, "user_id": "u1", "created_at": "2026-01-01T00:00:00Z"},
    ]
    services["extractor"].update_memory.return_value = (
        [
            {"id": "mem_1", "text": "Likes coffee", "event": "DELETE", "old_memory": ""},
        ],
        Usage(input_tokens=40, output_tokens=20, total_tokens=60),
    )

    svc = MemoryService(
        settings,
        services["session_mgr"],
        services["extractor"],
        services["embedding_client"],
        services["es_client"],
        redis_client=services["redis_client"],
        local_storage=services["local_storage"],
    )

    await svc._extract_and_store("u1", [
        {"role": "user", "content": "I actually dislike coffee"},
    ])

    services["es_client"].delete_document.assert_called_once_with(
        index=settings.es_index_name,
        doc_id="mem_1",
    )
