import redis.asyncio as aioredis
from memory_system.config import Settings


class RedisClient:
    def __init__(self, settings: Settings):
        self._url = settings.redis_url
        self._client: aioredis.Redis | None = None

    async def get_redis(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
