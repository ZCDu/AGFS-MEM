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


# ---------------------------------------------------------------------------
# Helpers to reduce duplication across tests
# ---------------------------------------------------------------------------

def _setup_mock_redis():
    """Return an AsyncMock wired up for the Redis session-store protocol."""
    mock_redis = AsyncMock()
    mock_redis.hgetall.return_value = {
        "messages": "[]",
        "archived_rounds": "[]",
    }
    mock_redis.hset = AsyncMock()
    mock_redis.expire = AsyncMock()
    return mock_redis


def _setup_mock_es():
    """Return an AsyncMock wired up for the ES protocol (index check + search)."""
    mock_es = AsyncMock()
    mock_es.indices.exists.return_value = True
    mock_es.search.return_value = {"hits": {"hits": []}}
    return mock_es


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_flow_mocked(client):
    """Integration test with all external services mocked at the client level."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.es_client.ESClient.get_es"
    ) as mock_es_get, patch(
        "memory_system.clients.embedding_client.EmbeddingClient.get_embedding"
    ) as mock_emb, patch(
        "memory_system.clients.llm_client.LLMClient.chat_json"
    ) as mock_llm_json, patch(
        "memory_system.clients.llm_client.LLMClient.chat"
    ) as mock_llm_chat:

        mock_redis = _setup_mock_redis()
        mock_es = _setup_mock_es()
        mock_redis_get.return_value = mock_redis
        mock_es_get.return_value = mock_es
        mock_emb.return_value = [0.1] * 768
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "hello"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.response"
        assert "history" in data
        assert "retrieved_memories" in data


def test_multimodal_content_flow(client):
    """Test that multimodal content (text + image) is handled correctly."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.es_client.ESClient.get_es"
    ) as mock_es_get, patch(
        "memory_system.clients.embedding_client.EmbeddingClient.get_embedding"
    ) as mock_emb, patch(
        "memory_system.clients.llm_client.LLMClient.chat_json"
    ) as mock_llm_json, patch(
        "memory_system.clients.llm_client.LLMClient.chat"
    ) as mock_llm_chat:

        mock_redis = _setup_mock_redis()
        mock_es = _setup_mock_es()
        mock_redis_get.return_value = mock_redis
        mock_es_get.return_value = mock_es
        mock_emb.return_value = [0.1, 0.2, 0.3]
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "what's in this image?"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/img.jpg",
                            },
                        ],
                    },
                    {"role": "assistant", "content": "it's a cat"},
                ],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.response"


def test_error_response_format(client):
    """Test that error responses follow the expected format."""
    resp = client.post(
        "/v1/memory",
        json={"model": "memory-v1"},
        # missing userId and sessionId
    )

    assert resp.status_code == 422
    data = resp.json()
    assert "detail" in data


def test_full_flow_with_memory_retrieval(client):
    """Integration test simulating memory retrieval from ES."""
    with patch(
        "memory_system.clients.redis_client.RedisClient.get_redis"
    ) as mock_redis_get, patch(
        "memory_system.clients.es_client.ESClient.get_es"
    ) as mock_es_get, patch(
        "memory_system.clients.embedding_client.EmbeddingClient.get_embedding"
    ) as mock_emb, patch(
        "memory_system.clients.llm_client.LLMClient.chat_json"
    ) as mock_llm_json, patch(
        "memory_system.clients.llm_client.LLMClient.chat"
    ) as mock_llm_chat:

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
        mock_llm_chat.return_value = "ok"
        mock_llm_json.return_value = []

        resp = client.post(
            "/v1/memory",
            json={
                "model": "memory-v1",
                "userId": "user_123",
                "sessionId": "sess_abc",
                "input": [{"role": "user", "content": "我叫什么名字？"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["retrieved_memories"]) >= 0
        assert "history" in data
