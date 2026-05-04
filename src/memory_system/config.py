from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    session_window_size: int = 10
    session_ttl_seconds: int = 86400
    archived_rounds_max: int = 50
    relevance_threshold: float = 0.7
    mem_importance_threshold: float = 0.5
    time_decay_lambda: float = 0.01
    mem_retrieval_top_k: int = 10

    redis_url: str = "redis://localhost:6379/0"
    es_url: str = "http://localhost:9200"
    embedding_api_url: str = ""
    embedding_dim: int = 768
    llm_api_url: str = ""
    llm_api_key: str = ""
