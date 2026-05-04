import httpx
from memory_system.config import Settings


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self._url = settings.embedding_api_url
        self._dim = settings.embedding_dim

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for a single text."""
        results = await self.get_embeddings([text])
        return results[0]

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts in one batch."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._url,
                json={"input": texts, "model": "embedding"},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Embedding API error: status={resp.status_code}, "
                    f"body={resp.text}"
                )
            data = resp.json()
            return [item["embedding"] for item in data["data"]]
