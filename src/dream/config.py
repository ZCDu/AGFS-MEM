"""DREAM runtime configuration loaded from environment variables."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Callable

from dream.curators.llm_backend import OpenAICuratorBackend, SemanticCuratorBackend
from dream.review.backend import DeterministicReviewBackend, ReviewBackend
from dream.review.llm_backend import OpenAIReviewBackend
from dream.writeback import (
    DeterministicWritebackBackend,
    OpenAIWritebackBackend,
    WritebackBackend,
)


@dataclass(frozen=True)
class InternshipSourceSettings:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    tenant_id: str = ""
    agent_id: str = ""
    batch_size: int = 100
    timeout_seconds: float = 15.0
    interval_seconds: int = 300


@dataclass(frozen=True)
class DreamSettings:
    home: str = "~/.dream"
    review_backend: str = "deterministic"
    review_model: str = ""
    review_base_url: str | None = None
    review_api_key: str = ""
    review_max_completion_tokens: int = 2000
    llm_structured_mode: str = "auto"
    curator_backend: str = "inherit"
    curator_model: str = ""
    curator_base_url: str | None = None
    curator_api_key: str = ""
    curator_max_completion_tokens: int = 3000
    character_definition_limit: int = 3200
    user_persona_limit: int = 1200
    internship_source: InternshipSourceSettings = field(
        default_factory=InternshipSourceSettings
    )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid .env entry at line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid .env key at line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _boolean(value: str, name: str) -> bool:
    normalized = value.casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def load_settings(path: Path | None = None) -> DreamSettings:
    """Load settings from a dotenv file, with process environment taking priority."""

    file_values = _read_env_file(path) if path is not None else {}

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    review_backend = value("DREAM_REVIEW_BACKEND", "deterministic").lower()
    if review_backend not in {"deterministic", "openai"}:
        raise ValueError("DREAM_REVIEW_BACKEND must be deterministic or openai")

    curator_backend = value("DREAM_CURATOR_BACKEND", "inherit").lower()
    if curator_backend not in {"inherit", "deterministic", "openai"}:
        raise ValueError(
            "DREAM_CURATOR_BACKEND must be inherit, deterministic, or openai"
        )
    structured_mode = value("DREAM_LLM_STRUCTURED_MODE", "auto").lower()
    if structured_mode not in {"auto", "tools", "json"}:
        raise ValueError("DREAM_LLM_STRUCTURED_MODE must be auto, tools, or json")

    source = InternshipSourceSettings(
        enabled=_boolean(
            value("DREAM_INTERNSHIP_SOURCE_ENABLED", "false"),
            "DREAM_INTERNSHIP_SOURCE_ENABLED",
        ),
        url=value("DREAM_INTERNSHIP_SOURCE_URL"),
        api_key=value("DREAM_INTERNSHIP_SOURCE_API_KEY"),
        tenant_id=value("DREAM_INTERNSHIP_SOURCE_TENANT_ID"),
        agent_id=value("DREAM_INTERNSHIP_SOURCE_AGENT_ID"),
        batch_size=_positive_int(
            value("DREAM_INTERNSHIP_SOURCE_BATCH_SIZE", "100"),
            "DREAM_INTERNSHIP_SOURCE_BATCH_SIZE",
        ),
        timeout_seconds=_positive_float(
            value("DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS", "15"),
            "DREAM_INTERNSHIP_SOURCE_TIMEOUT_SECONDS",
        ),
        interval_seconds=_positive_int(
            value("DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS", "300"),
            "DREAM_INTERNSHIP_SOURCE_INTERVAL_SECONDS",
        ),
    )
    if source.enabled and not all((source.url, source.tenant_id, source.agent_id)):
        raise ValueError(
            "enabled Internship source requires URL, tenant ID, and agent ID"
        )

    return DreamSettings(
        home=value("DREAM_HOME", "~/.dream"),
        review_backend=review_backend,
        review_model=value("DREAM_REVIEW_MODEL"),
        review_base_url=value("DREAM_REVIEW_BASE_URL") or None,
        review_api_key=value("DREAM_LLM_API_KEY"),
        review_max_completion_tokens=_positive_int(
            value("DREAM_REVIEW_MAX_COMPLETION_TOKENS", "2000"),
            "DREAM_REVIEW_MAX_COMPLETION_TOKENS",
        ),
        llm_structured_mode=structured_mode,
        curator_backend=curator_backend,
        curator_model=value("DREAM_CURATOR_MODEL"),
        curator_base_url=value("DREAM_CURATOR_BASE_URL") or None,
        curator_api_key=value("DREAM_CURATOR_LLM_API_KEY"),
        curator_max_completion_tokens=_positive_int(
            value("DREAM_CURATOR_MAX_COMPLETION_TOKENS", "3000"),
            "DREAM_CURATOR_MAX_COMPLETION_TOKENS",
        ),
        character_definition_limit=_positive_int(
            value("DREAM_CHARACTER_DEFINITION_LIMIT", "3200"),
            "DREAM_CHARACTER_DEFINITION_LIMIT",
        ),
        user_persona_limit=_positive_int(
            value("DREAM_USER_PERSONA_LIMIT", "1200"),
            "DREAM_USER_PERSONA_LIMIT",
        ),
        internship_source=source,
    )


def build_review_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
) -> ReviewBackend:
    if settings.review_backend == "deterministic":
        return DeterministicReviewBackend()
    if not settings.review_model:
        raise ValueError("DREAM_REVIEW_MODEL is required for the openai backend")
    if not settings.review_api_key:
        raise ValueError("missing LLM API key: DREAM_LLM_API_KEY")
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    client_kwargs: dict[str, object] = {
        "api_key": settings.review_api_key,
        "max_retries": 0,
    }
    if settings.review_base_url:
        client_kwargs["base_url"] = settings.review_base_url
    client = client_factory(**client_kwargs)
    return OpenAIReviewBackend(
        client=client,
        model=settings.review_model,
        max_completion_tokens=settings.review_max_completion_tokens,
        structured_mode=settings.llm_structured_mode,
    )


def build_curator_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
) -> SemanticCuratorBackend | None:
    backend = settings.curator_backend
    if backend == "deterministic":
        return None
    if backend == "inherit" and settings.review_backend == "deterministic":
        return None
    model = settings.curator_model or settings.review_model
    if not model:
        raise ValueError(
            "DREAM_CURATOR_MODEL or DREAM_REVIEW_MODEL is required for LLM curation"
        )
    api_key = settings.curator_api_key or settings.review_api_key
    if not api_key:
        raise ValueError(
            "missing LLM API key: DREAM_CURATOR_LLM_API_KEY or DREAM_LLM_API_KEY"
        )
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    base_url = settings.curator_base_url or settings.review_base_url
    client_kwargs: dict[str, object] = {"api_key": api_key, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = client_factory(**client_kwargs)
    return OpenAICuratorBackend(
        client=client,
        model=model,
        max_completion_tokens=settings.curator_max_completion_tokens,
        structured_mode=settings.llm_structured_mode,
    )


def build_writeback_backend(
    settings: DreamSettings,
    *,
    client_factory: Callable[..., object] | None = None,
) -> WritebackBackend:
    if settings.review_backend == "deterministic":
        return DeterministicWritebackBackend()
    if not settings.review_model or not settings.review_api_key:
        raise ValueError("writeback requires DREAM_REVIEW_MODEL and DREAM_LLM_API_KEY")
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    client_kwargs: dict[str, object] = {
        "api_key": settings.review_api_key,
        "max_retries": 0,
    }
    if settings.review_base_url:
        client_kwargs["base_url"] = settings.review_base_url
    client = client_factory(**client_kwargs)
    return OpenAIWritebackBackend(
        client=client,
        model=settings.review_model,
        structured_mode=settings.llm_structured_mode,
        max_completion_tokens=settings.curator_max_completion_tokens,
    )
