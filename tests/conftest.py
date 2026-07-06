import pytest
from memory_system.config import Settings


@pytest.fixture
def settings():
    return Settings(
        redis_url="redis://localhost:6379/0",
        embedding_api_url="http://localhost:8080/v1/embeddings",
        embedding_dim=768,
        llm_api_url="http://localhost:8081/v1",
        llm_api_key="test-key",
        session_window_size=3,
        session_ttl_seconds=86400,
        archived_rounds_max=10,
        relevance_threshold=0.7,
        memory_score_threshold=0.7,
        mem_retrieval_top_k=5,
    )
