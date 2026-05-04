import math
import uuid
from datetime import datetime, timezone
from memory_system.config import Settings


class LongTermMemory:
    def __init__(self, settings: Settings, embedding_client):
        self._settings = settings
        self._embedding = embedding_client

    @staticmethod
    def index_name(user_id: str) -> str:
        return f"memory_long_term_{user_id}"

    async def ensure_index(self, es, user_id: str):
        """Create ES index with dense_vector mapping if not exists."""
        name = self.index_name(user_id)
        exists = await es.indices.exists(index=name)
        if exists:
            return

        body = {
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "user_id": {"type": "keyword"},
                    "session_id": {"type": "keyword"},
                    "memory": {"type": "text"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": self._settings.embedding_dim,
                        "index": True,
                        "similarity": "cosine",
                    },
                    "memory_type": {"type": "keyword"},
                    "importance": {"type": "float"},
                    "metadata": {"type": "object", "enabled": False},
                    "created_at": {"type": "date"},
                }
            }
        }
        await es.indices.create(index=name, body=body)

    async def search(
        self, es, user_id: str, query_embedding: list[float]
    ) -> list[dict]:
        """kNN search with time decay in application layer."""
        await self.ensure_index(es, user_id)
        name = self.index_name(user_id)

        body = {
            "knn": {
                "field": "embedding",
                "query_vector": query_embedding,
                "k": self._settings.mem_retrieval_top_k,
                "num_candidates": self._settings.mem_retrieval_top_k * 5,
            }
        }
        resp = await es.search(index=name, body=body, source=True)

        results = []
        now = datetime.now(timezone.utc)
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            src["_id"] = hit["_id"]
            cosine_score = hit["_score"] or 0.0

            # Time decay
            try:
                created = datetime.fromisoformat(
                    src["created_at"].replace("Z", "+00:00")
                )
            except (ValueError, KeyError):
                created = now
            days_ago = (now - created).total_seconds() / 86400.0
            decay = math.exp(-self._settings.time_decay_lambda * days_ago)
            src["time_decayed_score"] = cosine_score * decay
            results.append(src)

        # Sort by time-decayed score
        results.sort(key=lambda x: x["time_decayed_score"], reverse=True)
        return results

    async def upsert_memory(self, es, user_id: str, doc: dict):
        """Insert or update a memory document."""
        await self.ensure_index(es, user_id)
        name = self.index_name(user_id)

        memory_id = doc.get("id") or uuid.uuid4().hex
        doc["id"] = memory_id
        doc["user_id"] = user_id
        doc.setdefault("created_at", datetime.now(timezone.utc).isoformat())

        # Compute embedding for the memory text
        emb = await self._embedding.get_embedding(doc["memory"])
        doc["embedding"] = emb

        await es.index(index=name, id=memory_id, body=doc, refresh=True)

    async def delete_memory(self, es, user_id: str, memory_id: str):
        """Delete a memory by id."""
        name = self.index_name(user_id)
        try:
            await es.delete(index=name, id=memory_id)
        except Exception:
            pass  # Already deleted or doesn't exist
