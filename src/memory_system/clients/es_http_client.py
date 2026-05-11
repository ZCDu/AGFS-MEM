import logging
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)


class ESHttpClient:
    """ES operations via HTTP REST (no elasticsearch-py dependency)."""

    def __init__(self, base_url: str, auth: tuple[str, str] | None = None, verify: bool = True):
        self._url = base_url.rstrip("/")
        self._auth = auth
        self._verify = verify

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self._verify)

    async def ensure_index(self, index: str, dims: int):
        """Create index with dense_vector mapping if it doesn't exist."""
        async with self._client() as client:
            resp = await client.head(f"{self._url}/{index}", auth=self._auth)
            if resp.status_code == 200:
                return

            mapping = {
                "settings": {"index": {"number_of_replicas": 1, "number_of_shards": 2, "refresh_interval": "1s"}},
                "mappings": {
                    "properties": {
                        "vector": {
                            "type": "dense_vector",
                            "dims": dims,
                            "index": True,
                            "similarity": "cosine",
                        },
                        "data": {"type": "text"},
                        "user_id": {"type": "keyword"},
                        "hash": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "updated_at": {"type": "date"},
                    }
                },
            }
            resp = await client.put(f"{self._url}/{index}", json=mapping, auth=self._auth)
            if resp.status_code not in (200, 201):
                logger.warning(f"Failed to create ES index: {resp.text}")

    async def index_document(
        self, index: str, doc_id: str, vector: list[float], data: str,
        user_id: str, hash_val: str,
    ):
        """Store a single memory with embedding vector."""
        async with self._client() as client:
            now = datetime.now(timezone.utc).isoformat()
            body = {
                "vector": vector,
                "data": data,
                "user_id": user_id,
                "hash": hash_val,
                "created_at": now,
                "updated_at": now,
            }
            resp = await client.put(
                f"{self._url}/{index}/_doc/{doc_id}", json=body, auth=self._auth,
            )
            if resp.status_code not in (200, 201):
                logger.warning(f"ES index failed: {resp.text}")

    async def search_knn(
        self, index: str, vector: list[float], k: int = 10,
        user_id: str | None = None,
    ) -> list[dict]:
        """KNN vector search, optionally filtered by user_id."""
        knn: dict = {
            "field": "vector",
            "query_vector": vector,
            "k": k,
            "num_candidates": k * 2,
        }
        if user_id:
            knn["filter"] = {"bool": {"must": [{"term": {"user_id": user_id}}]}}

        async with self._client() as client:
            resp = await client.post(
                f"{self._url}/{index}/_search",
                json={"knn": knn},
                auth=self._auth,
            )
            if resp.status_code != 200:
                logger.warning(f"ES search failed: {resp.text}")
                return []

            hits = resp.json().get("hits", {}).get("hits", [])
            results = []
            for h in hits:
                src = h["_source"]
                results.append({
                    "id": h["_id"],
                    "memory": src.get("data", ""),
                    "score": h["_score"],
                    "user_id": src.get("user_id", ""),
                    "created_at": src.get("created_at", ""),
                })
            return results

    async def update_document(
        self, index: str, doc_id: str, vector: list[float], data: str,
    ):
        """Update an existing memory's text and vector."""
        async with self._client() as client:
            body = {
                "doc": {
                    "vector": vector,
                    "data": data,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }
            resp = await client.post(
                f"{self._url}/{index}/_update/{doc_id}", json=body, auth=self._auth,
            )
            if resp.status_code not in (200, 201):
                logger.warning(f"ES update failed: {resp.text}")

    async def delete_document(self, index: str, doc_id: str):
        async with self._client() as client:
            await client.delete(f"{self._url}/{index}/_doc/{doc_id}", auth=self._auth)
