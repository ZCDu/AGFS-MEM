import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.es_client import ESClient


@pytest.fixture
def es_client(settings):
    return ESClient(settings)


@pytest.mark.asyncio
async def test_get_es_returns_client(es_client):
    with patch("memory_system.clients.es_client.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        result = await es_client.get_es()
        assert result is mock_instance
        mock_es_cls.assert_called_once()


@pytest.mark.asyncio
async def test_get_es_reuses_connection(es_client):
    with patch("memory_system.clients.es_client.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        c1 = await es_client.get_es()
        c2 = await es_client.get_es()
        assert c1 is c2
        mock_es_cls.assert_called_once()


@pytest.mark.asyncio
async def test_close(es_client):
    with patch("memory_system.clients.es_client.AsyncElasticsearch") as mock_es_cls:
        mock_instance = AsyncMock()
        mock_es_cls.return_value = mock_instance

        await es_client.get_es()
        await es_client.close()
        mock_instance.close.assert_called_once()
