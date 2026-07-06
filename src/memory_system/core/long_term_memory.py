import hashlib
import logging

from memory_system.api.models import RetrievedMemory, Usage
from memory_system.config import Settings

logger = logging.getLogger(__name__)


class LongTermMemoryEngine:
    """Extract, reconcile, store, and retrieve long-term memories."""

    def __init__(self, settings: Settings, extractor, embedding_client, es_client):
        self._settings = settings
        self._extractor = extractor
        self._embedding = embedding_client
        self._es = es_client

    @staticmethod
    def memory_doc_id(user_id: str, memory_text: str) -> str:
        normalized = " ".join(memory_text.split())
        return hashlib.sha256(f"{user_id}\0{normalized}".encode()).hexdigest()

    async def store_messages(self, user_id: str, raw_messages: list[dict]) -> Usage:
        """Extract facts, search similar memories, decide actions, and persist."""
        total_usage = Usage()

        try:
            facts, ext_usage = await self._extractor.extract(raw_messages)
            total_usage += ext_usage
            if not facts:
                return total_usage

            old_memories, fact_embeddings = await self._find_similar_memories(user_id, facts)

            if not old_memories:
                await self._add_new_facts(user_id, facts, fact_embeddings)
                logger.info(
                    "Added %s new memories for user=%s, usage=%s",
                    len(facts),
                    user_id,
                    total_usage,
                )
                return total_usage

            actions, upd_usage = await self._extractor.update_memory(old_memories, facts)
            total_usage += upd_usage
            if not actions:
                return total_usage

            await self._execute_actions(user_id, actions)
            self._log_action_counts(user_id, actions, total_usage)

        except Exception as e:
            logger.error("Long-term memory store failed for user=%s: %s", user_id, e, exc_info=True)

        return total_usage

    async def retrieve(self, user_id: str, query_text: str) -> list[RetrievedMemory]:
        """Return threshold-filtered long-term memories for a query."""
        if not query_text:
            return []

        try:
            query_embedding, _ = await self._embedding.get_embedding(query_text)
        except Exception as e:
            logger.warning("Failed to get query embedding: %s", e)
            return []

        try:
            hits = await self._es.search_knn(
                index=self._settings.es_index_name,
                vector=query_embedding,
                k=self._settings.mem_retrieval_top_k,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("ES search failed: %s", e)
            return []

        return self._to_retrieved_memories(hits)

    async def _find_similar_memories(
        self,
        user_id: str,
        facts: list[str],
    ) -> tuple[list[dict], dict[str, list[float]]]:
        old_memories_map: dict[str, dict] = {}
        fact_embeddings: dict[str, list[float]] = {}
        for fact in facts:
            try:
                embedding, _ = await self._embedding.get_embedding(fact)
                fact_embeddings[fact] = embedding
                hits = await self._es.search_knn(
                    index=self._settings.es_index_name,
                    vector=embedding,
                    k=self._settings.mem_retrieval_top_k,
                    user_id=user_id,
                )
                for hit in hits:
                    doc_id = hit.get("id", "")
                    if doc_id and doc_id not in old_memories_map:
                        old_memories_map[doc_id] = {
                            "id": doc_id,
                            "text": hit.get("memory", ""),
                        }
            except Exception as e:
                logger.error("Failed to search similar for fact '%s': %s", fact, e)
        return list(old_memories_map.values()), fact_embeddings

    async def _add_new_facts(
        self,
        user_id: str,
        facts: list[str],
        fact_embeddings: dict[str, list[float]],
    ) -> None:
        for fact in facts:
            try:
                embedding = fact_embeddings[fact]
                doc_id = self.memory_doc_id(user_id, fact)
                await self._es.index_document(
                    index=self._settings.es_index_name,
                    doc_id=doc_id,
                    vector=embedding,
                    data=fact,
                    user_id=user_id,
                    hash_val=doc_id,
                )
            except Exception as e:
                logger.error("Failed to ADD fact '%s': %s", fact, e)

    async def _execute_actions(self, user_id: str, actions: list[dict]) -> None:
        for action in actions:
            event = action.get("event", "").upper()
            memory_text = action.get("text", "")
            memory_id = action.get("id", "")

            try:
                if event in ("ADD", "UPDATE"):
                    embedding, _ = await self._embedding.get_embedding(memory_text)

                if event == "ADD":
                    doc_id = self.memory_doc_id(user_id, memory_text)
                    await self._es.index_document(
                        index=self._settings.es_index_name,
                        doc_id=doc_id,
                        vector=embedding,
                        data=memory_text,
                        user_id=user_id,
                        hash_val=doc_id,
                    )
                elif event == "UPDATE":
                    if not memory_id:
                        logger.warning("UPDATE action missing id, skipping")
                        continue
                    await self._es.update_document(
                        index=self._settings.es_index_name,
                        doc_id=memory_id,
                        vector=embedding,
                        data=memory_text,
                    )
                elif event == "DELETE":
                    if not memory_id:
                        logger.warning("DELETE action missing id, skipping")
                        continue
                    await self._es.delete_document(
                        index=self._settings.es_index_name,
                        doc_id=memory_id,
                    )
                elif event == "NONE":
                    pass
                else:
                    logger.warning("Unknown event '%s' for memory '%s'", event, memory_id)
            except Exception as e:
                logger.error("Failed to execute %s for memory '%s': %s", event, memory_id, e)

    def _to_retrieved_memories(self, hits: list[dict]) -> list[RetrievedMemory]:
        retrieved_memories = []
        for hit in hits:
            score = hit.get("score", 0.0)
            if score < self._settings.memory_score_threshold:
                continue
            memory = hit.get("memory", "")
            if not memory:
                continue
            retrieved_memories.append(
                RetrievedMemory(
                    id=hit.get("id", ""),
                    memory=memory,
                    score=score,
                    created_at=str(hit.get("created_at", "")),
                    importance=score,
                )
            )
        return retrieved_memories

    @staticmethod
    def _log_action_counts(user_id: str, actions: list[dict], usage: Usage) -> None:
        counts = {}
        for action in actions:
            event = action.get("event", "NONE").upper()
            counts[event] = counts.get(event, 0) + 1
        logger.info("Memory update complete for user=%s: %s, usage=%s", user_id, counts, usage)
