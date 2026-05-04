from elasticsearch import AsyncElasticsearch
from memory_system.config import Settings


class ESClient:
    def __init__(self, settings: Settings):
        self._url = settings.es_url
        self._client: AsyncElasticsearch | None = None

    async def get_es(self) -> AsyncElasticsearch:
        if self._client is None:
            self._client = AsyncElasticsearch(self._url)
        return self._client

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None
