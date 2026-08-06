import asyncio
from dataclasses import replace
import re

import httpx
import pytest
from redis.exceptions import RedisError

from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.service.app import create_app
from short_term_memory.service.memory_service import (
    MemoryReadUnavailableError,
    RetryableWriteError,
)
from short_term_memory.service.metrics import ApiMetrics
from short_term_memory.service.schemas import (
    HeadroomProxyContext,
    MemoryReadResponse,
    MemoryReadState,
    MemoryWriteResponse,
    ReadTiming,
    WriteTiming,
)
from short_term_memory.storage.async_redis_memory_store import EventConflictError
from short_term_memory.storage.journal_store import JournalConflictError
from tests.factories import read_payload, write_payload


class RecordingMemoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str]] = []
        self.next_error: BaseException | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def write(self, request, request_id):
        self.calls.append(("write", request, request_id))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.next_error is not None:
            raise self.next_error
        return MemoryWriteResponse(
            request_id=request_id,
            accepted=True,
            sequence_from=1,
            sequence_through=1,
            duplicate_event_ids=[],
            compression_queued=False,
            policy_version="v1",
            timing_ms=WriteTiming(total=4.0, redis=1.0, journal=2.0, queue=0.5),
        )

    async def read(self, request, request_id):
        self.calls.append(("read", request, request_id))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.next_error is not None:
            raise self.next_error
        return MemoryReadResponse(
            request_id=request_id,
            messages=[{"role": "user", "content": "recent original"}],
            memory=MemoryReadState(
                compressed_through_sequence=0,
                latest_sequence=1,
                source="redis",
                compression_segments=0,
            ),
            headroom=HeadroomProxyContext(
                proxy_url="http://headroom:8787/v1",
                scope_headers={"x-headroom-session-id": "opaque"},
            ),
            effective_config=None,
            timing_ms=ReadTiming(total=3.0, redis=1.0, recovery=0.0, assembly=0.5),
        )


def settings(
    *,
    token: str = "test-token",
    environment: str = "production",
    concurrency: int = 100,
    max_body_bytes: int = 10 * 1024 * 1024,
) -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(environment=environment)
    return replace(
        base,
        api=replace(
            base.api,
            auth_token=token,
            concurrency_limit=concurrency,
            max_body_bytes=max_body_bytes,
        ),
    )


def app_for(service: RecordingMemoryService, **setting_overrides):
    return create_app(
        lambda: service,
        settings=settings(**setting_overrides),
        metrics=ApiMetrics(),
    )


def auth_headers(**extra: str) -> dict[str, str]:
    return {"authorization": "Bearer test-token", **extra}


@pytest.mark.asyncio
async def test_only_two_business_routes_exist_and_openapi_documents_auth() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        schema = (await client.get("/openapi.json")).json()

    business = sorted(
        path for path in schema["paths"] if path.startswith("/v1/memories/")
    )
    assert business == ["/v1/memories/read", "/v1/memories/write"]
    for path in business:
        operation = schema["paths"][path]["post"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert {"401", "413", "422", "429", "500", "503"} <= set(operation["responses"])
        assert operation["responses"]["422"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorResponse")


@pytest.mark.asyncio
async def test_write_and_read_contract_call_only_the_injected_memory_service() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        written = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )
        read = await client.post(
            "/v1/memories/read", headers=auth_headers(), json=read_payload()
        )

    assert written.status_code == 200
    assert written.json()["accepted"] is True
    assert read.status_code == 200
    assert read.json()["headroom"]["proxy_url"].endswith("/v1")
    assert [call[0] for call in service.calls] == ["write", "read"]
    assert all(
        call[2] == response.headers["x-request-id"]
        for call, response in zip(service.calls, (written, read))
    )


@pytest.mark.asyncio
async def test_missing_and_wrong_auth_return_identical_sanitized_401() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post("/v1/memories/read", json=read_payload())
        wrong = await client.post(
            "/v1/memories/read",
            headers={"authorization": "Bearer private-wrong-token"},
            json=read_payload(),
        )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"] == wrong.json()["error"] == "unauthorized"
    assert set(missing.json()) == set(wrong.json()) == {"error", "request_id"}
    assert (
        missing.headers["www-authenticate"]
        == wrong.headers["www-authenticate"]
        == "Bearer"
    )
    assert "private-wrong-token" not in wrong.text


def test_app_construction_enforces_auth_environment_policy() -> None:
    with pytest.raises(ValueError, match="MEMORY_API_AUTH_TOKEN"):
        app_for(RecordingMemoryService(), token="", environment="production")

    app_for(RecordingMemoryService(), token="", environment="development")


@pytest.mark.asyncio
async def test_request_id_is_bounded_sanitized_and_always_returned() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.get("/health", headers={"x-request-id": "client.req-7"})
        control = await client.get("/health", headers={"x-request-id": "bad\x01value"})
        oversized = await client.get("/health", headers={"x-request-id": "x" * 1024})
        missing = await client.get("/does-not-exist")

    assert accepted.headers["x-request-id"] == "client.req-7"
    for response in (control, oversized, missing):
        generated = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", generated)
        assert len(generated) == 32


@pytest.mark.asyncio
async def test_content_length_limit_rejects_before_body_parsing() -> None:
    service = RecordingMemoryService()
    app = app_for(service, max_body_bytes=64)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={**auth_headers(), "content-type": "application/json"},
            content=b"{" + (b"SENSITIVE" * 20),
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert "SENSITIVE" not in response.text
    assert service.calls == []


@pytest.mark.asyncio
async def test_streaming_body_limit_rejects_without_content_length() -> None:
    service = RecordingMemoryService()
    app = app_for(service, max_body_bytes=20)

    async def chunks():
        yield b'{"private":"'
        yield b"STREAMING-SECRET-TOO-LARGE"
        yield b'"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={**auth_headers(), "content-type": "application/json"},
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert "STREAMING-SECRET" not in response.text
    assert service.calls == []


@pytest.mark.asyncio
async def test_overload_is_immediate_and_happens_before_json_parsing() -> None:
    service = RecordingMemoryService()
    service.release = asyncio.Event()
    app = app_for(service, concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        active = asyncio.create_task(
            client.post(
                "/v1/memories/write", headers=auth_headers(), json=write_payload()
            )
        )
        await asyncio.wait_for(service.entered.wait(), timeout=1)
        overloaded = await asyncio.wait_for(
            client.post(
                "/v1/memories/write",
                headers={**auth_headers(), "content-type": "application/json"},
                content=b"not-json",
            ),
            timeout=0.2,
        )
        service.release.set()
        completed = await active

    assert overloaded.status_code == 429
    assert overloaded.json()["error"] == "overloaded"
    assert overloaded.headers["retry-after"] == "1"
    assert completed.status_code == 200


@pytest.mark.asyncio
async def test_cancelled_request_releases_concurrency_capacity() -> None:
    service = RecordingMemoryService()
    service.release = asyncio.Event()
    app = app_for(service, concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        cancelled = asyncio.create_task(
            client.post(
                "/v1/memories/write", headers=auth_headers(), json=write_payload()
            )
        )
        await asyncio.wait_for(service.entered.wait(), timeout=1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        service.release.set()
        next_response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )

    assert next_response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (EventConflictError("ORIGINAL PRIVATE MESSAGE"), 409, "event_id_conflict"),
        (JournalConflictError("ORIGINAL PRIVATE MESSAGE"), 409, "event_id_conflict"),
        (RetryableWriteError("private-event", ()), 503, "service_unavailable"),
        (
            MemoryReadUnavailableError("ORIGINAL PRIVATE MESSAGE"),
            503,
            "service_unavailable",
        ),
        (RedisError("redis://user:private-password@host"), 503, "service_unavailable"),
        (RuntimeError("ORIGINAL PRIVATE MESSAGE"), 500, "internal_error"),
    ],
)
async def test_exceptions_map_to_stable_sanitized_errors(error, status, code) -> None:
    service = RecordingMemoryService()
    service.next_error = error
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )

    assert response.status_code == status
    assert response.json() == {
        "error": code,
        "request_id": response.headers["x-request-id"],
    }
    assert "ORIGINAL PRIVATE MESSAGE" not in response.text
    assert "private-password" not in response.text
    assert "private-event" not in response.text


@pytest.mark.asyncio
async def test_validation_error_is_sanitized_422_and_service_is_not_called() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers=auth_headers(),
            json={"user_id": "u", "session_id": "s", "events": []},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert set(response.json()) == {"error", "request_id"}
    assert service.calls == []


@pytest.mark.asyncio
async def test_configured_write_batch_limit_maps_to_validation_error() -> None:
    service = RecordingMemoryService()
    custom = settings()
    custom = replace(custom, api=replace(custom.api, write_max_batch_events=1))
    app = create_app(lambda: service, settings=custom, metrics=ApiMetrics())
    payload = write_payload()
    payload["events"].append({**payload["events"][0], "event_id": "event-2"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=payload
        )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert service.calls == []


@pytest.mark.asyncio
async def test_metrics_are_content_free_and_have_no_high_cardinality_labels() -> None:
    service = RecordingMemoryService()
    metrics = ApiMetrics()
    app = create_app(lambda: service, settings=settings(), metrics=metrics)
    payload = write_payload(content="SECRET_ANCHOR")
    payload["user_id"] = "SECRET-USER"
    payload["session_id"] = "SECRET-SESSION"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers=auth_headers(**{"x-request-id": "SECRET-REQUEST"}),
            json=payload,
        )
        rendered = (await client.get("/metrics")).text

    assert response.status_code == 200
    for secret in ("SECRET_ANCHOR", "SECRET-USER", "SECRET-SESSION", "SECRET-REQUEST"):
        assert secret not in rendered
    assert 'route="/v1/memories/write"' in rendered
    for stage in ("total", "redis", "journal", "queue"):
        assert f'stage="{stage}"' in rendered
