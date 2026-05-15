import pytest
from unittest.mock import AsyncMock, Mock, patch
from memory_system.clients.embedding_client import EmbeddingClient


@pytest.fixture
def emb_client(settings):
    return EmbeddingClient(settings)


@pytest.mark.asyncio
async def test_get_embedding_success(emb_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        embedding, usage = await emb_client.get_embedding("hello world")
        assert len(embedding) == 3
        assert embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_get_embedding_batch(emb_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {"embedding": [0.1, 0.2]},
            {"embedding": [0.3, 0.4]},
        ]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        embeddings, usage = await emb_client.get_embeddings(["text1", "text2"])
        assert len(embeddings) == 2
        assert embeddings[0] == [0.1, 0.2]


@pytest.mark.asyncio
async def test_get_embedding_api_error(emb_client):
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="Embedding API error"):
            await emb_client.get_embedding("test")
