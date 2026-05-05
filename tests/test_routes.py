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


def test_store_endpoint(client):
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        from memory_system.api.models import MemoryStoreResponse
        mock_svc.store.return_value = MemoryStoreResponse()
        mock_get_svc.return_value = mock_svc

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


def test_recall_endpoint(client):
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        from memory_system.api.models import MemoryRecallResponse
        mock_svc.recall.return_value = MemoryRecallResponse(
            model="memory-v1",
            history=[{"role": "user", "content": "hello"}],
            retrieved_memories=[],
            usage={"total_tokens": 5},
        )
        mock_get_svc.return_value = mock_svc

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


def test_missing_user_id(client):
    resp = client.post(
        "/v1/memory/store",
        json={
            "model": "memory-v1",
            "sessionId": "sess_abc",
            "input": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 422


def test_multimodal_content(client):
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        from memory_system.api.models import MemoryStoreResponse
        mock_svc.store.return_value = MemoryStoreResponse()
        mock_get_svc.return_value = mock_svc

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
                    }
                ],
            },
        )

        assert resp.status_code == 200


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
