import pytest
from unittest.mock import AsyncMock, patch
from memory_system.clients.redis_client import RedisClient


@pytest.fixture
def redis_client(settings):
    return RedisClient(settings)


@pytest.mark.asyncio
async def test_get_redis_returns_client(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        result = await redis_client.get_redis()
        assert result is mock_client
        mock_from_url.assert_called_once()


@pytest.mark.asyncio
async def test_get_redis_reuses_connection(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        c1 = await redis_client.get_redis()
        c2 = await redis_client.get_redis()
        assert c1 is c2
        mock_from_url.assert_called_once()


@pytest.mark.asyncio
async def test_close(redis_client):
    with patch("redis.asyncio.from_url") as mock_from_url:
        mock_client = AsyncMock()
        mock_from_url.return_value = mock_client

        await redis_client.get_redis()
        await redis_client.close()
        mock_client.aclose.assert_called_once()
