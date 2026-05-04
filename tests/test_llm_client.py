import pytest
from unittest.mock import AsyncMock, Mock, patch
from memory_system.clients.llm_client import LLMClient


@pytest.fixture
def llm_client(settings):
    return LLMClient(settings)


@pytest.mark.asyncio
async def test_chat_success(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello!"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.chat(
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="Be helpful",
        )
        assert result == "Hello!"


@pytest.mark.asyncio
async def test_chat_json_mode(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"key": "value"}'}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        result = await llm_client.chat_json(
            messages=[{"role": "user", "content": "extract"}],
            system_prompt="Return JSON",
        )
        assert result == {"key": "value"}


@pytest.mark.asyncio
async def test_chat_json_invalid_response(llm_client):
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json"}}]
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="Failed to parse LLM JSON"):
            await llm_client.chat_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="Return JSON",
            )


@pytest.mark.asyncio
async def test_chat_api_error(llm_client):
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.text = "Server Error"

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        with pytest.raises(RuntimeError, match="LLM API error"):
            await llm_client.chat([{"role": "user", "content": "hi"}])
