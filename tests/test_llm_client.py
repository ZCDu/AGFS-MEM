import pytest
from unittest.mock import AsyncMock, Mock, patch
from memory_system.clients.llm_client import (
    DeepSeekProvider,
    LLMClient,
    OpenAICompatibleProvider,
    create_chat_model_provider,
)


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

        content, usage = await llm_client.generate_json("Be helpful", "hi")
        assert content == "Hello!"
        body = mock_http.post.await_args.kwargs["json"]
        assert body["response_format"] == {"type": "json_object"}


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

        result, usage = await llm_client.extract_json_field("Return JSON", "extract", field="facts")
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

        result, usage = await llm_client.extract_json_field("Return JSON", "test", field="facts")
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


def test_create_chat_model_provider_openai_compatible():
    provider = create_chat_model_provider(
        provider="openai-compatible",
        base_url="http://localhost:8081/v1",
        api_key="test-key",
        model="",
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._model == "qwen-plus"


def test_create_chat_model_provider_deepseek_defaults():
    provider = create_chat_model_provider(
        provider="deepseek",
        base_url="",
        api_key="test-key",
        model="",
    )

    assert isinstance(provider, DeepSeekProvider)
    assert provider._url == "https://api.deepseek.com"
    assert provider._model == "deepseek-chat"


def test_create_chat_model_provider_rejects_unknown():
    with pytest.raises(ValueError):
        create_chat_model_provider(
            provider="unknown",
            base_url="",
            api_key="",
            model="",
        )


@pytest.mark.asyncio
async def test_deepseek_provider_posts_to_chat_completions():
    provider = DeepSeekProvider(base_url="", api_key="deepseek-key", model="")
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"facts": []}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_cls.return_value.__aenter__.return_value = mock_http

        content, usage = await provider.generate_json("Return JSON", "Input")

    assert content == '{"facts": []}'
    assert usage["total_tokens"] == 3
    call = mock_http.post.await_args
    assert call.args[0] == "https://api.deepseek.com/chat/completions"
    assert call.kwargs["headers"]["Authorization"] == "Bearer deepseek-key"
    assert call.kwargs["json"]["model"] == "deepseek-chat"
