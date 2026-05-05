import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from memory_system.main import create_app
from memory_system.config import Settings


@pytest.fixture
def test_settings():
    return Settings(
        redis_url="redis://localhost:6379/0",
        es_url="http://localhost:9200",
        embedding_api_url="http://localhost:8080/v1/embeddings",
        embedding_dim=768,
        llm_api_url="http://localhost:8081/v1",
        llm_api_key="test-key",
        session_window_size=3,
        session_ttl_seconds=86400,
        archived_rounds_max=10,
        relevance_threshold=0.7,
        mem_importance_threshold=0.5,
        time_decay_lambda=0.01,
        mem_retrieval_top_k=5,
    )


@pytest.fixture
def app(test_settings):
    return create_app(test_settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def _setup_mock_redis():
    mock_redis = AsyncMock()
    mock_redis.hgetall.return_value = {
        "messages": "[]",
        "archived_rounds": "[]",
    }
    mock_redis.hset = AsyncMock()
    mock_redis.expire = AsyncMock()
    return mock_redis


def _setup_mock_es():
    mock_es = AsyncMock()
    mock_es.indices.exists.return_value = True
    mock_es.search.return_value = {"hits": {"hits": []}}
    return mock_es


# ---------------------------------------------------------------------------
# Store endpoint
# ---------------------------------------------------------------------------


def test_store_flow(client):
    """Store should save messages and return lightweight confirmation."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.llm_client.LLMClient.chat_json"
    ) as mock_llm_json, patch(
        "memory_system.clients.llm_client.LLMClient.chat"
    ) as mock_llm_chat:

        mock_redis = _setup_mock_redis()
        mock_redis_get.return_value = mock_redis
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory/store",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "hello"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.store"
        assert data["status"] == "stored"


def test_store_multimodal(client):
    """Store should accept multimodal content."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.llm_client.LLMClient.chat_json"
    ) as mock_llm_json, patch(
        "memory_system.clients.llm_client.LLMClient.chat"
    ) as mock_llm_chat:

        mock_redis = _setup_mock_redis()
        mock_redis_get.return_value = mock_redis
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory/store",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what's this?"},
                            {"type": "input_image", "image_url": "https://example.com/img.jpg"},
                        ],
                    },
                    {"role": "assistant", "content": "it's a cat"},
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json()["object"] == "memory.store"


def test_store_validation_error(client):
    """Missing required fields should return 422."""
    resp = client.post(
        "/v1/memory/store",
        json={"model": "memory-v1"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# Recall endpoint
# ---------------------------------------------------------------------------


def test_recall_flow(client):
    """Recall should return history from session context."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.es_client.ESClient.get_es"
    ) as mock_es_get, patch(
        "memory_system.clients.embedding_client.EmbeddingClient.get_embedding"
    ) as mock_emb:

        mock_redis = _setup_mock_redis()
        mock_es = _setup_mock_es()
        mock_redis_get.return_value = mock_redis
        mock_es_get.return_value = mock_es
        mock_emb.return_value = [0.1] * 768

        resp = client.post(
            "/v1/memory/recall",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "hello"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.recall"
        assert "history" in data
        assert "retrieved_memories" in data


def test_recall_with_memory_retrieval(client):
    """Recall should include long-term memories retrieved from ES."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.es_client.ESClient.get_es"
    ) as mock_es_get, patch(
        "memory_system.clients.embedding_client.EmbeddingClient.get_embedding"
    ) as mock_emb:

        mock_redis = _setup_mock_redis()
        mock_redis_get.return_value = mock_redis

        mock_es = AsyncMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "mem_1",
                        "_score": 0.95,
                        "_source": {
                            "memory": "用户叫张三，今年30岁",
                            "importance": 0.9,
                            "created_at": "2026-05-04T10:00:00Z",
                        },
                    }
                ]
            }
        }
        mock_es_get.return_value = mock_es

        mock_emb.return_value = [0.1, 0.2, 0.3]

        resp = client.post(
            "/v1/memory/recall",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "我叫什么名字？"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data


def test_recall_validation_error(client):
    """Missing required fields should return 422."""
    resp = client.post(
        "/v1/memory/recall",
        json={"model": "memory-v1"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()
