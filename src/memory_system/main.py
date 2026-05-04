from contextlib import asynccontextmanager
from fastapi import FastAPI
from memory_system.config import Settings
from memory_system.api.routes import router, set_memory_service
from memory_system.core.memory_service import MemoryService
from memory_system.core.session_manager import SessionManager
from memory_system.core.long_term import LongTermMemory
from memory_system.core.extractor import MemoryExtractor
from memory_system.clients.redis_client import RedisClient
from memory_system.clients.es_client import ESClient
from memory_system.clients.embedding_client import EmbeddingClient
from memory_system.clients.llm_client import LLMClient


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # Clients
    redis_client = RedisClient(settings)
    es_client = ESClient(settings)
    embedding_client = EmbeddingClient(settings)
    llm_client = LLMClient(settings)

    # Core services
    session_manager = SessionManager(settings, embedding_client)
    long_term = LongTermMemory(settings, embedding_client)
    extractor = MemoryExtractor(settings, llm_client, embedding_client)
    memory_service = MemoryService(
        settings,
        session_manager,
        long_term,
        extractor,
        embedding_client,
        redis_client=redis_client,
        es_client=es_client,
    )

    set_memory_service(memory_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await redis_client.close()
        await es_client.close()

    app = FastAPI(title="Memory System", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
