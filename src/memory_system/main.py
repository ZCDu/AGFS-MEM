import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)

from memory_system.config import Settings
from memory_system.api.routes import router, set_memory_service
from memory_system.core.memory_service import MemoryService
from memory_system.core.extractor import MemoryExtractor
from memory_system.core.history_builder import HistoryBuilder
from memory_system.core.local_journal import LocalJournalPipeline
from memory_system.core.long_term_memory import LongTermMemoryEngine
from memory_system.core.session_manager import SessionManager
from memory_system.clients.redis_client import RedisClient
from memory_system.clients.embedding_client import EmbeddingClient
from memory_system.clients.llm_client import LLMClient
from memory_system.clients.es_http_client import ESHttpClient
from memory_system.storage.local_storage import LocalStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # HTTP clients
    llm_client = LLMClient(
        base_url=settings.llm_api_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
    )
    scheme = "https" if settings.es_use_ssl else "http"
    es_client = ESHttpClient(
        base_url=f"{scheme}://{settings.es_host}:{settings.es_port}",
        auth=(settings.es_user, settings.es_password.get_secret_value()) if settings.es_user else None,
        verify=settings.es_verify_certs,
    )
    embedding_client = EmbeddingClient(settings)
    redis_client = RedisClient(settings)

    # Storage backend (local or mirage)
    if settings.storage_backend == "mirage":
        from memory_system.storage.mirage_storage import MirageStorage
        local_storage = MirageStorage(settings.mirage_storage_path)
    else:
        local_storage = LocalStorage(settings.storage_base_path)

    # Core services
    extractor = MemoryExtractor(llm_client)
    session_manager = SessionManager(settings)
    long_term_memory = LongTermMemoryEngine(
        settings,
        extractor,
        embedding_client,
        es_client,
    )
    history_builder = HistoryBuilder(settings, session_manager, long_term_memory)
    local_journal = LocalJournalPipeline(local_storage)
    memory_service = MemoryService(
        settings,
        session_manager,
        extractor,
        embedding_client,
        es_client,
        redis_client=redis_client,
        local_storage=local_storage,
        long_term_memory=long_term_memory,
        history_builder=history_builder,
        local_journal=local_journal,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Ensure ES index exists
        await es_client.ensure_index(settings.es_index_name, settings.embedding_dim)
        yield
        await redis_client.close()
        if hasattr(local_storage, "close"):
            await local_storage.close()

    app = FastAPI(title="Memory System", version="0.2.0", lifespan=lifespan)
    app.state.memory_service = memory_service
    set_memory_service(memory_service)
    app.include_router(router)
    return app


app = None
_app_settings: Settings | None = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app(_app_settings)
    return app
