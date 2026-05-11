import asyncio
import hashlib
import logging
from memory_system.config import Settings
from memory_system.api.models import (
    MemoryRequest,
    MemoryStoreResponse,
    MemoryRecallResponse,
    RetrievedMemory,
)
from memory_system.utils.text_utils import get_last_user_query

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        session_manager,
        extractor,
        embedding_client,
        es_client,
        redis_client=None,
        local_storage=None,
    ):
        self._settings = settings
        self._session = session_manager
        self._extractor = extractor
        self._embedding = embedding_client
        self._es = es_client
        self._redis_client = redis_client
        self._storage = local_storage

    async def _get_redis(self):
        return await self._redis_client.get_redis()

    async def store(self, request: MemoryRequest) -> MemoryStoreResponse:
        """Save a conversation round and extract long-term memories."""
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]

        redis = await self._get_redis()

        # 1. Add messages to session
        await self._session.add_round(redis, user_id, session_id, raw_messages)

        # 2. Background: extract facts → embed → store in ES
        task = asyncio.create_task(
            self._extract_and_store(user_id, raw_messages)
        )
        task.add_done_callback(
            lambda t: logger.error(f"Background _extract_and_store failed: {t.exception()}", exc_info=True)
            if t.exception() else None
        )

        # 3. Background: persist to local storage
        if self._storage:
            task2 = asyncio.create_task(
                self._local_storage_pipeline(user_id, raw_messages)
            )
            task2.add_done_callback(
                lambda t: logger.error(f"Background _local_storage_pipeline failed: {t.exception()}", exc_info=True)
                if t.exception() else None
            )

        return MemoryStoreResponse(model=request.model, status="stored")

    async def recall(self, request: MemoryRequest) -> MemoryRecallResponse:
        """Retrieve session history + long-term memories for the current query."""
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]

        redis = await self._get_redis()

        # 1. Get query embedding
        query_text = get_last_user_query(raw_messages)
        query_embedding = []
        if query_text:
            try:
                query_embedding = await self._embedding.get_embedding(query_text)
            except Exception as e:
                logger.warning(f"Failed to get query embedding: {e}")

        # 2. Search ES via KNN
        es_hits = []
        if query_embedding:
            try:
                es_hits = await self._es.search_knn(
                    index=self._settings.es_index_name,
                    vector=query_embedding,
                    k=self._settings.mem_retrieval_top_k,
                    user_id=user_id,
                )
            except Exception as e:
                logger.warning(f"ES search failed: {e}")

        # 3. Build session history (uses bigram overlap coefficient, no embedding needed)
        recent_full = self._settings.recent_rounds_full
        if request.memory_settings and request.memory_settings.recent_rounds_full is not None:
            recent_full = request.memory_settings.recent_rounds_full
        history_data = await self._session.build_history(
            redis, user_id, session_id, query_text,
            recent_rounds_full=recent_full,
        )

        # 4. Build response — filter by relevance threshold
        threshold = self._settings.relevance_threshold
        retrieved_memories = []
        for hit in es_hits:
            score = hit.get("score", 0.0)
            if score < threshold:
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
                    importance=score,  # Use score as importance proxy
                )
            )
            history_data["history_messages"].insert(
                0,
                {"role": "system", "content": f"[Retrieved memory: {memory}]"},
            )

        return MemoryRecallResponse(
            model=request.model,
            history=history_data["history_messages"],
            retrieved_memories=retrieved_memories,
            usage={"total_tokens": sum(
                len(str(m.get("content", "")))
                for m in history_data.get("history_messages", [])
            )},
        )

    async def _extract_and_store(self, user_id: str, raw_messages: list[dict]):
        """Background: LLM extraction → search similar → decide ADD/UPDATE/DELETE.

        Follows mem0's memory update pattern:
        1. Extract new facts via LLM
        2. For each fact, embed and KNN-search ES for similar existing memories
        3. If no similar memories exist, directly ADD all facts
        4. If similar memories exist, ask LLM to decide ADD/UPDATE/DELETE/NONE
        """
        try:
            facts = await self._extractor.extract(raw_messages)
            if not facts:
                return

            # Collect similar existing memories per fact via KNN search
            old_memories_map: dict[str, dict] = {}  # doc_id -> {id, text}
            for fact in facts:
                try:
                    embedding = await self._embedding.get_embedding(fact)
                    hits = await self._es.search_knn(
                        index=self._settings.es_index_name,
                        vector=embedding,
                        k=self._settings.mem_retrieval_top_k,
                        user_id=user_id,
                    )
                    for h in hits:
                        doc_id = h.get("id", "")
                        if doc_id and doc_id not in old_memories_map:
                            old_memories_map[doc_id] = {
                                "id": doc_id,
                                "text": h.get("memory", ""),
                            }
                except Exception as e:
                    logger.error(f"Failed to search similar for fact '{fact}': {e}")

            old_memories = list(old_memories_map.values())

            # No existing similar memories — directly ADD all facts
            if not old_memories:
                for fact in facts:
                    try:
                        embedding = await self._embedding.get_embedding(fact)
                        doc_id = hashlib.md5(fact.encode()).hexdigest()
                        await self._es.index_document(
                            index=self._settings.es_index_name,
                            doc_id=doc_id,
                            vector=embedding,
                            data=fact,
                            user_id=user_id,
                            hash_val=doc_id,
                        )
                    except Exception as e:
                        logger.error(f"Failed to ADD fact '{fact}': {e}")
                logger.info(f"Added {len(facts)} new memories for user={user_id}")
                return

            # Has similar memories — ask LLM to decide actions
            actions = await self._extractor.update_memory(old_memories, facts)
            if not actions:
                return

            for action in actions:
                event = action.get("event", "").upper()
                memory_text = action.get("text", "")
                memory_id = action.get("id", "")

                try:
                    if event == "ADD":
                        embedding = await self._embedding.get_embedding(memory_text)
                        doc_id = memory_id or hashlib.md5(memory_text.encode()).hexdigest()
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
                        embedding = await self._embedding.get_embedding(memory_text)
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
                        pass  # No change needed

                    else:
                        logger.warning(f"Unknown event '{event}' for memory '{memory_id}'")

                except Exception as e:
                    logger.error(
                        f"Failed to execute {event} for memory '{memory_id}': {e}"
                    )

            counts = {}
            for a in actions:
                e = a.get("event", "NONE").upper()
                counts[e] = counts.get(e, 0) + 1
            logger.info(
                f"Memory update complete for user={user_id}: {counts}"
            )

        except Exception as e:
            logger.error(f"_extract_and_store failed for user={user_id}: {e}", exc_info=True)

    async def _local_storage_pipeline(
        self, user_id: str, messages: list[dict],
    ):
        """Background task: save files locally and write journal entry."""
        file_attachments = self._storage._extract_files(messages)

        file_map: dict[str, str] = {}
        for fa in file_attachments:
            local_path = await self._storage.save_file(user_id, fa["file_url"])
            if local_path:
                file_map[fa["file_url"]] = local_path

        entries = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if item.get("type") == "input_text" and item.get("text"):
                        text_parts.append(item["text"])
                    elif item.get("type") == "file" and item.get("file_url"):
                        url = item["file_url"]
                        local = file_map.get(url, url)
                        entries.append({
                            "type": "file",
                            "original_url": url,
                            "local_path": local,
                        })
                content = " ".join(text_parts)

            if isinstance(content, str) and content.strip():
                entries.append({
                    "type": "message",
                    "role": msg.get("role", "unknown"),
                    "content": content,
                })

        if entries:
            await self._storage.write_journal(user_id, entries)
