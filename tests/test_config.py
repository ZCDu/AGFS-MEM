from pathlib import Path

import pytest

from dream.config import (
    DreamSettings,
    build_curator_backend,
    build_review_backend,
    load_settings,
)
from dream.api import create_app
from dream.curators.llm_backend import OpenAICuratorBackend
from dream.review.backend import DeterministicReviewBackend
from dream.review.llm_backend import OpenAIReviewBackend


def test_missing_env_file_uses_safe_deterministic_backend(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / ".env")
    assert isinstance(build_review_backend(settings), DeterministicReviewBackend)


def test_internship_source_defaults_to_disabled(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / ".env")
    assert settings.internship_source.enabled is False


def test_internship_source_loads_pull_settings(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_INTERNSHIP_SOURCE_ENABLED=true\n"
        "DREAM_INTERNSHIP_SOURCE_URL=http://127.0.0.1:8000/v1/memory/dream-export\n"
        "DREAM_INTERNSHIP_SOURCE_API_KEY=source-secret\n"
        "DREAM_INTERNSHIP_SOURCE_TENANT_ID=acme\n"
        "DREAM_INTERNSHIP_SOURCE_AGENT_ID=assistant\n"
        "DREAM_INTERNSHIP_SOURCE_BATCH_SIZE=25\n"
        "DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS=7.5\n"
        "DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS=120\n",
        encoding="utf-8",
    )
    source = load_settings(env_file).internship_source
    assert source.enabled is True
    assert source.url.endswith("/v1/memory/dream-export")
    assert source.api_key == "source-secret"
    assert source.tenant_id == "acme"
    assert source.agent_id == "assistant"
    assert source.batch_size == 25
    assert source.timeout_seconds == 7.5
    assert source.interval_seconds == 120


def test_openai_compatible_backend_uses_env_file_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_REVIEW_BASE_URL=https://llm.example/v1\n"
        "DREAM_LLM_API_KEY=secret-key\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def client_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    backend = build_review_backend(
        load_settings(env_file), client_factory=client_factory
    )

    assert isinstance(backend, OpenAIReviewBackend)
    assert backend.model == "review-model"
    assert captured == {
        "api_key": "secret-key",
        "base_url": "https://llm.example/v1",
        "max_retries": 0,
    }


def test_process_environment_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DREAM_REVIEW_MODEL=file-model\n", encoding="utf-8")
    monkeypatch.setenv("DREAM_REVIEW_MODEL", "process-model")

    assert load_settings(env_file).review_model == "process-model"


def test_openai_backend_rejects_missing_api_key() -> None:
    settings = DreamSettings(
        review_backend="openai",
        review_model="review-model",
        review_base_url=None,
        review_api_key="",
        review_max_completion_tokens=2000,
    )
    with pytest.raises(ValueError, match="DREAM_LLM_API_KEY"):
        build_review_backend(settings, client_factory=lambda **_: object())


def test_api_loads_review_backend_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=review-model\n"
        "DREAM_LLM_API_KEY=secret-key\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path,
        env_file=env_file,
        client_factory=lambda **_: object(),
    )
    assert isinstance(app.state.dream_service.reviewer.backend, OpenAIReviewBackend)
    assert isinstance(
        app.state.dream_service.semantic_curator_backend, OpenAICuratorBackend
    )


def test_curator_backend_can_inherit_the_review_model(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=shared-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_CURATOR_BACKEND=inherit\n",
        encoding="utf-8",
    )
    backend = build_curator_backend(
        load_settings(env_file), client_factory=lambda **_: object()
    )
    assert isinstance(backend, OpenAICuratorBackend)
    assert backend.model == "shared-model"


def test_structured_llm_mode_is_loaded_and_passed_to_backends(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DREAM_REVIEW_BACKEND=openai\n"
        "DREAM_REVIEW_MODEL=agnes-model\n"
        "DREAM_LLM_API_KEY=secret-key\n"
        "DREAM_LLM_STRUCTURED_MODE=json\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file)

    review = build_review_backend(settings, client_factory=lambda **_: object())
    curator = build_curator_backend(settings, client_factory=lambda **_: object())

    assert settings.llm_structured_mode == "json"
    assert review.structured_mode == "json"
    assert curator is not None
    assert curator.structured_mode == "json"
