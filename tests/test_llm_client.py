import pytest
from unittest.mock import AsyncMock, Mock, patch
from memory_system.clients.llm_client import LLMClient


@pytest.fixture
def llm_client():
    return LLMClient(
        base_url="http://localhost:8081/v1",
        api_key="test-key",
        model="qwen-plus",
    )


@pytest.mark.asyncio
async def test_generate_json_success(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello!"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.generate_json("Be helpful", "hi")
        assert result == "Hello!"


@pytest.mark.asyncio
async def test_extract_json_field(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"facts": ["fact1", "fact2"]}'}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.extract_json_field("Return JSON", "extract", field="facts")
        assert result == ["fact1", "fact2"]


@pytest.mark.asyncio
async def test_extract_json_invalid_response(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.extract_json_field("Return JSON", "test", field="facts")
        assert result == []


@pytest.mark.asyncio
async def test_generate_json_api_error(llm_client):
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.text = "Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(Exception):
            await llm_client.generate_json("Be helpful", "hi")
