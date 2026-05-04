from memory_system.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.session_window_size == 10
    assert s.session_ttl_seconds == 86400
    assert s.relevance_threshold == 0.7
    assert s.embedding_dim == 768


def test_settings_custom_values():
    s = Settings(
        redis_url="redis://custom:6379",
        session_window_size=5,
        embedding_dim=1024,
    )
    assert s.redis_url == "redis://custom:6379"
    assert s.session_window_size == 5
    assert s.embedding_dim == 1024
