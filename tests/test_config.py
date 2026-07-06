import pytest
from pydantic import SecretStr, ValidationError

from memory_system.config import Settings, resolve_env_files


def test_settings_defaults():
    s = Settings()
    assert s.session_window_size == 10
    assert s.session_ttl_seconds == 86400
    assert s.relevance_threshold == 0.35
    assert s.memory_score_threshold == 1.2
    assert s.embedding_dim == 1024
    assert s.history_compression_strategy == "reversible"
    assert isinstance(s.llm_provider, str)
    assert isinstance(s.llm_model, str)
    # .env file may override defaults; just verify it's a string
    assert isinstance(s.llm_api_key.get_secret_value(), str)


def test_resolve_env_files_defaults_to_dotenv(monkeypatch):
    monkeypatch.delenv("MEMORY_ENV", raising=False)
    monkeypatch.delenv("MEMORY_ENV_FILE", raising=False)

    assert resolve_env_files() == ".env"


def test_resolve_env_files_uses_named_environment(monkeypatch):
    monkeypatch.setenv("MEMORY_ENV", "test")
    monkeypatch.delenv("MEMORY_ENV_FILE", raising=False)

    assert resolve_env_files() == (".env", ".env.test")


def test_resolve_env_files_explicit_file_wins(monkeypatch):
    monkeypatch.setenv("MEMORY_ENV", "test")
    monkeypatch.setenv("MEMORY_ENV_FILE", "/tmp/custom.env")

    assert resolve_env_files() == "/tmp/custom.env"


def test_settings_loads_named_environment_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORY_ENV", "test")
    monkeypatch.delenv("MEMORY_ENV_FILE", raising=False)

    (tmp_path / ".env").write_text(
        "REDIS_URL=redis://base:6379/0\n"
        "LLM_MODEL=base-model\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.test").write_text(
        "REDIS_URL=redis://test:6379/0\n",
        encoding="utf-8",
    )

    s = Settings()

    assert s.redis_url == "redis://test:6379/0"
    assert s.llm_model == "base-model"


def test_settings_loads_explicit_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("REDIS_URL=redis://custom-env-file:6379/0\n", encoding="utf-8")
    monkeypatch.setenv("MEMORY_ENV", "test")
    monkeypatch.setenv("MEMORY_ENV_FILE", str(env_file))

    s = Settings()

    assert s.redis_url == "redis://custom-env-file:6379/0"


def test_settings_custom_values():
    s = Settings(
        redis_url="redis://custom:6379",
        session_window_size=5,
        embedding_dim=768,
        llm_api_key="sk-test-key",
    )
    assert s.redis_url == "redis://custom:6379"
    assert s.session_window_size == 5
    assert s.embedding_dim == 768
    assert s.llm_api_key.get_secret_value() == "sk-test-key"


def test_secret_str_not_leaked_in_repr():
    """SecretStr must not expose the raw value in repr/str."""
    s = Settings(llm_api_key="super-secret-12345")
    repr_str = repr(s.llm_api_key)
    assert "super-secret-12345" not in repr_str
    assert s.llm_api_key.get_secret_value() == "super-secret-12345"


@pytest.mark.parametrize(
    "field,invalid_value",
    [
        ("session_window_size", 0),
        ("session_window_size", -1),
        ("session_ttl_seconds", 0),
        ("embedding_dim", 0),
        ("mem_retrieval_top_k", 0),
        ("relevance_threshold", -0.1),
        ("relevance_threshold", 1.1),
        ("memory_score_threshold", -0.1),
        ("memory_score_threshold", 2.1),
    ],
)
def test_field_constraints_reject_invalid(field, invalid_value):
    """Numeric fields with validation constraints should reject out-of-range values."""
    with pytest.raises(ValidationError):
        Settings(**{field: invalid_value})
