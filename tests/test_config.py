from pathlib import Path

import pytest

from short_term_memory.config import load_settings


ENV_NAMES = (
    "SHORT_TERM_MEMORY_HOME",
    "SHORT_TERM_MEMORY_ENV",
    "SHORT_TERM_MEMORY_SCOPE_SECRET",
    "REDIS_URL",
    "REDIS_SESSION_TTL_SECONDS",
    "REDIS_HISTORY_TURNS",
    "CONTEXT_WINDOW_TOKENS",
    "HEADROOM_SERVICE_URL",
    "HEADROOM_SERVICE_TIMEOUT_SECONDS",
    "HEADROOM_COMPRESSION_MODEL",
    "HEADROOM_CCR_TTL_SECONDS",
    "HEADROOM_TRIGGER_RATIO",
    "HEADROOM_MAX_MESSAGES",
    "HEADROOM_MAX_SESSION_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_short_term_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_default_short_term_settings(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env")

    assert settings.environment == "development"
    assert settings.home == "~/.dream"
    assert settings.redis_session.url == "redis://127.0.0.1:6379/0"
    assert settings.redis_session.ttl_seconds == 43_200
    assert settings.redis_session.history_turns == 10
    assert settings.redis_session.trigger_ratio == 0.65
    assert settings.headroom_service.ccr_ttl_seconds == 43_200


def test_process_environment_overrides_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("REDIS_HISTORY_TURNS=5\n", encoding="utf-8")
    monkeypatch.setenv("REDIS_HISTORY_TURNS", "12")

    assert load_settings(path).redis_session.history_turns == 12


def test_production_requires_headroom_url(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "SHORT_TERM_MEMORY_SCOPE_SECRET=secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="HEADROOM_SERVICE_URL"):
        load_settings(path)


def test_production_requires_scope_secret(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "HEADROOM_SERVICE_URL=http://127.0.0.1:8787\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SHORT_TERM_MEMORY_SCOPE_SECRET"):
        load_settings(path)


def test_production_accepts_complete_external_service_config(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SHORT_TERM_MEMORY_ENV=production\n"
        "SHORT_TERM_MEMORY_SCOPE_SECRET=secret\n"
        "HEADROOM_SERVICE_URL=http://headroom:8787/\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.environment == "production"
    assert settings.headroom_service.url == "http://headroom:8787"


@pytest.mark.parametrize("ratio", ["0.59", "0.71"])
def test_trigger_ratio_must_match_plan(tmp_path: Path, ratio: str) -> None:
    path = tmp_path / ".env"
    path.write_text(f"HEADROOM_TRIGGER_RATIO={ratio}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        load_settings(path)
