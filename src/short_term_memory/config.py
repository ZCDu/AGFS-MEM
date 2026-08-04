"""Configuration for the standalone short-term memory SDK."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class RedisSessionSettings:
    url: str = "redis://127.0.0.1:6379/0"
    ttl_seconds: int = 43_200
    history_turns: int = 10
    context_window_tokens: int = 128_000
    trigger_ratio: float = 0.65
    max_messages: int = 100
    max_session_seconds: int = 14_400


@dataclass(frozen=True)
class HeadroomServiceSettings:
    url: str = ""
    timeout_seconds: float = 300.0
    compression_model: str = "gpt-4o"
    ccr_ttl_seconds: int = 43_200


@dataclass(frozen=True)
class ShortTermMemorySettings:
    environment: str = "development"
    home: str = "~/.dream"
    optimization_scope_secret: str = "development-only-scope-secret"
    redis_session: RedisSessionSettings = field(default_factory=RedisSessionSettings)
    headroom_service: HeadroomServiceSettings = field(
        default_factory=HeadroomServiceSettings
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


def _plan_trigger_ratio(value: str, name: str) -> float:
    parsed = _positive_float(value, name)
    if not 0.60 <= parsed <= 0.70:
        raise ValueError(f"{name} must be between 0.60 and 0.70")
    return parsed


def _http_service_url(value: str, name: str) -> str:
    if not value:
        return ""
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP URL")
    return normalized


def load_settings(path: Path | None = None) -> ShortTermMemorySettings:
    """Load dotenv values with process environment taking priority."""

    file_values = _read_env_file(path) if path is not None else {}

    def value(name: str, default: str = "") -> str:
        return os.environ.get(name, file_values.get(name, default)).strip()

    environment = value("SHORT_TERM_MEMORY_ENV", "development").casefold()
    if environment not in {"development", "production"}:
        raise ValueError(
            "SHORT_TERM_MEMORY_ENV must be development or production"
        )

    headroom = HeadroomServiceSettings(
        url=_http_service_url(
            value("HEADROOM_SERVICE_URL", ""), "HEADROOM_SERVICE_URL"
        ),
        timeout_seconds=_positive_float(
            value("HEADROOM_SERVICE_TIMEOUT_SECONDS", "300"),
            "HEADROOM_SERVICE_TIMEOUT_SECONDS",
        ),
        compression_model=value("HEADROOM_COMPRESSION_MODEL", "gpt-4o"),
        ccr_ttl_seconds=_positive_int(
            value("HEADROOM_CCR_TTL_SECONDS", "43200"),
            "HEADROOM_CCR_TTL_SECONDS",
        ),
    )
    if environment == "production" and not headroom.url:
        raise ValueError("HEADROOM_SERVICE_URL is required in production")

    scope_secret = value(
        "SHORT_TERM_MEMORY_SCOPE_SECRET", "development-only-scope-secret"
    )
    if environment == "production" and not value(
        "SHORT_TERM_MEMORY_SCOPE_SECRET"
    ):
        raise ValueError(
            "production requires SHORT_TERM_MEMORY_SCOPE_SECRET"
        )

    redis_session = RedisSessionSettings(
        url=value("REDIS_URL", "redis://127.0.0.1:6379/0"),
        ttl_seconds=_positive_int(
            value("REDIS_SESSION_TTL_SECONDS", "43200"),
            "REDIS_SESSION_TTL_SECONDS",
        ),
        history_turns=_positive_int(
            value("REDIS_HISTORY_TURNS", "10"), "REDIS_HISTORY_TURNS"
        ),
        context_window_tokens=_positive_int(
            value("CONTEXT_WINDOW_TOKENS", "128000"),
            "CONTEXT_WINDOW_TOKENS",
        ),
        trigger_ratio=_plan_trigger_ratio(
            value("HEADROOM_TRIGGER_RATIO", "0.65"),
            "HEADROOM_TRIGGER_RATIO",
        ),
        max_messages=_positive_int(
            value("HEADROOM_MAX_MESSAGES", "100"),
            "HEADROOM_MAX_MESSAGES",
        ),
        max_session_seconds=_positive_int(
            value("HEADROOM_MAX_SESSION_SECONDS", "14400"),
            "HEADROOM_MAX_SESSION_SECONDS",
        ),
    )

    return ShortTermMemorySettings(
        environment=environment,
        home=value("SHORT_TERM_MEMORY_HOME", "~/.dream"),
        optimization_scope_secret=scope_secret,
        redis_session=redis_session,
        headroom_service=headroom,
    )
