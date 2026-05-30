import pytest
from pydantic import SecretStr, ValidationError

from memory_system.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.session_window_size == 10
    assert s.session_ttl_seconds == 86400
    assert s.relevance_threshold == 0.35
    assert s.memory_score_threshold == 1.2
    assert s.embedding_dim == 1024
    # .env file may override defaults; just verify it's a string
    assert isinstance(s.llm_api_key.get_secret_value(), str)


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
