import pytest
from pydantic import SecretStr, ValidationError

from memory_system.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.session_window_size == 10
    assert s.session_ttl_seconds == 86400
    assert s.relevance_threshold == 0.7
    assert s.embedding_dim == 768
    # SecretStr comparison: compare the revealed value, not the object
    assert s.llm_api_key.get_secret_value() == ""


def test_settings_custom_values():
    s = Settings(
        redis_url="redis://custom:6379",
        session_window_size=5,
        embedding_dim=1024,
        llm_api_key="sk-test-key",
    )
    assert s.redis_url == "redis://custom:6379"
    assert s.session_window_size == 5
    assert s.embedding_dim == 1024
    assert s.llm_api_key.get_secret_value() == "sk-test-key"


def test_secret_str_not_leaked_in_repr():
    """SecretStr must not expose the raw value in repr/str."""
    s = Settings(llm_api_key="super-secret-12345")
    repr_str = repr(s.llm_api_key)
    assert "super-secret-12345" not in repr_str
    # The actual value is still retrievable via get_secret_value
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
        ("mem_importance_threshold", -0.01),
        ("mem_importance_threshold", 1.01),
        ("time_decay_lambda", 0),
        ("time_decay_lambda", -0.5),
    ],
)
def test_field_constraints_reject_invalid(field, invalid_value):
    """Numeric fields with validation constraints should reject out-of-range values."""
    with pytest.raises(ValidationError):
        Settings(**{field: invalid_value})
