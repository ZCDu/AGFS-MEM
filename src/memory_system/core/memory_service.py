import asyncio
import logging
from memory_system.config import Settings
from memory_system.api.models import (
    MemoryRequest,
    MemoryStoreResponse,
    MemoryRecallResponse,
    Usage,
)
from memory_system.core.history_builder import HistoryBuilder
from memory_system.core.local_journal import LocalJournalPipeline
from memory_system.core.long_term_memory import LongTermMemoryEngine

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
        long_term_memory=None,
        history_builder=None,
        local_journal=None,
    ):
        self._settings = settings
        self._session = session_manager
        self._redis_client = redis_client
        self._storage = local_storage
        self._long_term_memory = long_term_memory or LongTermMemoryEngine(
            settings,
            extractor,
            embedding_client,
            es_client,
        )
        self._history_builder = history_builder or HistoryBuilder(
            settings,
            session_manager,
            self._long_term_memory,
        )
        self._local_journal = local_journal or (
            LocalJournalPipeline(local_storage) if local_storage else None
        )

    async def _get_redis(self):
        return await self._redis_client.get_redis()

    @staticmethod
    def _memory_doc_id(user_id: str, memory_text: str) -> str:
        return LongTermMemoryEngine.memory_doc_id(user_id, memory_text)

    async def store(self, request: MemoryRequest) -> MemoryStoreResponse:
        """Save a conversation round and extract long-term memories."""
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]

        redis = await self._get_redis()

        # 1. Add messages to session
        await self._session.add_round(redis, user_id, session_id, raw_messages)

        # 2. Extract + store in long-term memory (inline so usage can be returned)
        usage = await self._extract_and_store(user_id, raw_messages)

        # 3. Background: persist attachments and journal entries
        if self._local_journal:
            asyncio.create_task(
                self._local_storage_pipeline(user_id, raw_messages)
            ).add_done_callback(
                lambda t: logger.error(
                    f"Background _local_storage_pipeline failed: {t.exception()}",
                    exc_info=True,
                ) if t.exception() else None
            )

        return MemoryStoreResponse(model=request.model, status="stored", usage=usage)

    async def recall(self, request: MemoryRequest) -> MemoryRecallResponse:
        """Retrieve session history + long-term memories for the current query."""
        user_id = request.userId
        session_id = request.sessionId
        raw_messages = [msg.model_dump() for msg in request.input]
        redis = await self._get_redis()

        history, retrieved_memories = await self._history_builder.build(
            redis,
            user_id,
            session_id,
            raw_messages,
            memory_settings=request.memory_settings,
        )
        return MemoryRecallResponse(
            model=request.model,
            history=history,
            retrieved_memories=retrieved_memories,
            usage=Usage(),
        )

    async def retrieve_context(self, context_hash: str) -> dict | None:
        redis = await self._get_redis()
        return await self._session.retrieve_context(redis, context_hash)

    async def _extract_and_store(self, user_id: str, raw_messages: list[dict]) -> Usage:
        """Backward-compatible wrapper around the long-term memory module."""
        return await self._long_term_memory.store_messages(user_id, raw_messages)

    async def _local_storage_pipeline(
        self, user_id: str, messages: list[dict],
    ):
        """Backward-compatible wrapper around the local journal pipeline."""
        if self._local_journal:
            await self._local_journal.save_messages(user_id, messages)
