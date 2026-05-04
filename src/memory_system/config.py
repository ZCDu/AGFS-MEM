from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    session_window_size: int = Field(default=10, gt=0)
    session_ttl_seconds: int = Field(default=86400, gt=0)
    archived_rounds_max: int = 50
    relevance_threshold: float = Field(default=0.7, ge=0, le=1)
    mem_importance_threshold: float = Field(default=0.5, ge=0, le=1)
    time_decay_lambda: float = Field(default=0.01, gt=0)
    mem_retrieval_top_k: int = Field(default=10, gt=0)

    redis_url: str = "redis://localhost:6379/0"
    es_url: str = "http://localhost:9200"
    embedding_api_url: str = ""
    embedding_dim: int = Field(default=768, gt=0)
    llm_api_url: str = ""
    llm_api_key: SecretStr = Field(default=SecretStr(""))
