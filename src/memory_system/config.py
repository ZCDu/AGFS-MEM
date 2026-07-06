import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


def resolve_env_files():
    """Resolve dotenv files from environment selection knobs.

    MEMORY_ENV_FILE=/abs/or/relative/path loads that file only.
    MEMORY_ENV=test loads .env first, then .env.test as an override.
    """
    explicit_file = os.getenv("MEMORY_ENV_FILE")
    if explicit_file:
        return explicit_file

    env_name = os.getenv("MEMORY_ENV")
    if env_name:
        return (".env", f".env.{env_name}")

    return ".env"


class Settings(BaseSettings):
    model_config = {"env_file_encoding": "utf-8", "extra": "ignore"}

    def __init__(self, **values):
        values.setdefault("_env_file", resolve_env_files())
        super().__init__(**values)

    # Session
    session_window_size: int = Field(default=10, gt=0)
    session_ttl_seconds: int = Field(default=86400, gt=0)
    archived_rounds_max: int = 50
    recent_rounds_full: int = Field(default=3, gt=0)
    history_compression_strategy: str = Field(default="reversible")
    context_compression_ttl_seconds: int = Field(default=86400, gt=0)

    # Search
    relevance_threshold: float = Field(default=0.35, ge=0, le=1)
    memory_score_threshold: float = Field(default=1.2, ge=0, le=2)
    mem_retrieval_top_k: int = Field(default=10, gt=0)

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Embedding (used by SessionManager + mem0)
    embedding_api_url: str = ""
    embedding_api_key: SecretStr = Field(default=SecretStr(""))
    embedding_dim: int = Field(default=1024, gt=0)

    # LLM (used by mem0)
    llm_provider: str = "openai-compatible"
    llm_api_url: str = ""
    llm_api_key: SecretStr = Field(default=SecretStr(""))
    llm_model: str = ""

    # Elasticsearch (mem0 vector store)
    es_host: str = "localhost"
    es_port: int = 9200
    es_user: str = ""
    es_password: SecretStr = Field(default=SecretStr(""))
    es_use_ssl: bool = False
    es_verify_certs: bool = False
    es_index_name: str = "mem0"

    # Local storage
    storage_backend: str = "local"
    storage_base_path: str = "~/memory_system_data"
    mirage_storage_path: str = "~/memory_mirage_data"
