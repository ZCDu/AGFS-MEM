import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.es_http_client import ESHttpClient


@pytest.fixture
def es_client():
    return ESHttpClient(base_url="http://localhost:9200", auth=("user", "pass"))


@pytest.mark.asyncio
async def test_ensure_index_creates(es_client):
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http

        head_resp = AsyncMock()
        head_resp.status_code = 404
        put_resp = AsyncMock()
        put_resp.status_code = 200
        mock_http.head.return_value = head_resp
        mock_http.put.return_value = put_resp

        await es_client.ensure_index("test_index", 1024)

        mock_http.head.assert_called_once()
        mock_http.put.assert_called_once()
        call_args = mock_http.put.call_args
        body = call_args[1]["json"]
        assert body["mappings"]["properties"]["vector"]["dims"] == 1024


@pytest.mark.asyncio
async def test_ensure_index_skips_existing(es_client):
    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http

        head_resp = AsyncMock()
        head_resp.status_code = 200
        mock_http.head.return_value = head_resp

        await es_client.ensure_index("test_index", 1024)

        mock_http.put.assert_not_called()
