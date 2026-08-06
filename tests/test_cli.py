import asyncio
from dataclasses import replace

import pytest

from short_term_memory import cli
from short_term_memory.config import ShortTermMemorySettings


def configured_settings() -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(environment="development")
    return replace(
        base,
        headroom_service=replace(base.headroom_service, url="http://headroom:8787"),
        api=replace(
            base.api,
            host="0.0.0.0",
            port=9090,
            workers=4,
            concurrency_limit=64,
        ),
    )


def test_api_cli_uses_factory_workers_and_at_least_100_concurrency(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "load_settings", configured_settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.api_main([])

    args, kwargs = calls[0]
    assert args == ("short_term_memory.service.runtime:create_runtime_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9090
    assert kwargs["workers"] == 4
    assert kwargs["limit_concurrency"] == 100


def test_worker_cli_does_not_swallow_configuration_error(monkeypatch) -> None:
    def invalid_settings():
        raise ValueError("invalid runtime configuration")

    monkeypatch.setattr(cli, "load_settings", invalid_settings)

    with pytest.raises(ValueError, match="invalid runtime configuration"):
        cli.worker_main([])


def test_worker_cli_runs_async_worker_process(monkeypatch) -> None:
    observed = []
    settings = configured_settings()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    def run(coroutine):
        observed.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(asyncio, "run", run)

    cli.worker_main([])

    assert len(observed) == 1
