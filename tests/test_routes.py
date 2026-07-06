import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from memory_system.main import create_app
from memory_system.config import Settings


@pytest.fixture
def test_settings():
    return Settings(
        redis_url="redis://localhost:6379/0",
        embedding_api_url="http://localhost:8080/v1/embeddings",
        embedding_dim=768,
        llm_api_url="http://localhost:8081/v1",
        llm_api_key="test-key",
        session_window_size=3,
        session_ttl_seconds=86400,
        archived_rounds_max=10,
        relevance_threshold=0.7,
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
        from memory_system.api.models import MemoryRecallResponse, HistoryMessage, Usage
        mock_svc.recall.return_value = MemoryRecallResponse(
            model="memory-v1",
            history=[HistoryMessage(role="user", content="hello")],
            retrieved_memories=[],
            usage=Usage(total_tokens=5),
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


def test_retrieve_context_endpoint(client):
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        mock_svc.retrieve_context.return_value = {
            "hash": "abc123",
            "user_id": "user_123",
            "session_id": "sess_abc",
            "round_id": "r1",
            "messages": [{"role": "assistant", "content": "full answer"}],
            "query_text": "query",
            "created_at": "2026-06-07T00:00:00+00:00",
        }
        mock_get_svc.return_value = mock_svc

        resp = client.get("/v1/memory/context/abc123")

        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "memory.context"
        assert data["messages"][0]["content"] == "full answer"


def test_retrieve_context_endpoint_404(client):
    with patch(
        "memory_system.api.routes.get_memory_service"
    ) as mock_get_svc:
        mock_svc = AsyncMock()
        mock_svc.retrieve_context.return_value = None
        mock_get_svc.return_value = mock_svc

        resp = client.get("/v1/memory/context/missing")

        assert resp.status_code == 404


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


def test_app_exposes_memory_service_on_state(app):
    assert app.state.memory_service is not None
