import asyncio
import logging
from datetime import datetime, timezone
from memory_system.config import Settings
from memory_system.api.models import (
    MemoryRequest,
    MemoryResponse,
    RetrievedMemory,
)
from memory_system.utils.text_utils import (
    messages_to_text,
    get_last_user_query,
)

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        session_manager,
        long_term_memory,
        memory_extractor,
        embedding_client,
        redis_client=None,
        es_client=None,
    ):
        self._settings = settings
        self._session = session_manager
        self._ltm = long_term_memory
        self._extractor = memory_extractor
        self._embedding = embedding_client
        self._redis_client = redis_client
        self._es_client = es_client

    async def _get_redis(self):
        return await self._redis_client.get_redis()

    async def _get_es(self):
        return await self._es_client.get_es()

    async def process(self, request: MemoryRequest) -> MemoryResponse:
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]

        redis = await self._get_redis()
        es = await self._get_es()

        # 1. Get last user query and its embedding
        query_text = get_last_user_query(raw_messages)
        query_embedding = []
        if query_text:
            try:
                query_embedding = await self._embedding.get_embedding(query_text)
            except Exception as e:
                logger.warning(f"Failed to get query embedding: {e}")

        # 2. Parallel: search long-term memories + add round to session
        retrieved_raw = []
        if query_embedding:
            async_tasks = [
                self._ltm.search(es, user_id, query_embedding),
                self._session.add_round(redis, user_id, session_id, raw_messages),
            ]
            results = await asyncio.gather(*async_tasks, return_exceptions=True)
            if isinstance(results[0], list):
                retrieved_raw = results[0]
            else:
                logger.warning(f"LTM search failed: {results[0]}")
        else:
            await self._session.add_round(redis, user_id, session_id, raw_messages)

        # 3. Build session history
        history_data = await self._session.build_history(
            redis, user_id, session_id, query_embedding
        )

        # 4. Prepend retrieved memories to history
        retrieved_memories = []
        for mem in retrieved_raw:
            retrieved_memories.append(
                RetrievedMemory(
                    id=mem.get("_id", ""),
                    memory=mem.get("memory", ""),
                    score=mem.get("time_decayed_score", 0.0),
                    created_at=mem.get("created_at", ""),
                    importance=mem.get("importance", 0.0),
                )
            )
            # Insert retrieved memory into history
            history_data["history_messages"].insert(
                0,
                {
                    "role": "system",
                    "content": f"[Retrieved memory: {mem.get('memory', '')}]",
                },
            )

        # 5. Background: extract memories from any archived rounds
        session = await self._session.get_session(redis, user_id, session_id)
        if session["archived_rounds"]:
            asyncio.create_task(
                self._archive_pipeline(
                    es, user_id, session_id, session["archived_rounds"]
                )
            )

        # 6. Build response
        output_text = messages_to_text(history_data["history_messages"])

        return MemoryResponse(
            model=request.model,
            output_text=output_text,
            history=history_data["history_messages"],
            retrieved_memories=retrieved_memories,
            usage={"total_tokens": len(output_text.split())},
        )

    async def _archive_pipeline(
        self, es, user_id: str, session_id: str, archived_rounds: list
    ):
        """Background task: extract and store long-term memories."""
        for entry in archived_rounds:
            try:
                messages = entry["messages"]
                memories = await self._extractor.extract_memories(messages)
                if not memories:
                    continue

                # Check existing similar memories
                combined = " ".join(m.get("content", "") for m in memories)
                emb = await self._embedding.get_embedding(combined)
                existing = await self._ltm.search(es, user_id, emb)

                # Resolve conflicts
                actions = await self._extractor.resolve_conflicts(memories, existing)

                for action in actions:
                    act = action.get("action")
                    if act == "add":
                        nm = action.get("new_memory", {})
                        doc = {
                            "session_id": session_id,
                            "memory": nm.get("content", ""),
                            "memory_type": nm.get("type", "fact"),
                            "importance": nm.get("importance", 0.5),
                            "metadata": {"source_round": entry.get("round_id", "")},
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self._ltm.upsert_memory(es, user_id, doc)

                    elif act == "update":
                        old_id = action.get("old_id", "")
                        doc = {
                            "id": old_id,
                            "session_id": session_id,
                            "memory": action.get("new_content", ""),
                            "memory_type": "fact",
                            "importance": action.get("new_importance", 0.5),
                            "metadata": {"source_round": entry.get("round_id", "")},
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self._ltm.upsert_memory(es, user_id, doc)

                    elif act == "delete":
                        old_id = action.get("old_id", "")
                        await self._ltm.delete_memory(es, user_id, old_id)

                    # skip: do nothing

            except Exception as e:
                logger.error(f"Archive pipeline error for round: {e}")
