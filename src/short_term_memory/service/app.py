"""FastAPI application exposing the two memory-service business routes."""

from collections.abc import Awaitable, Callable
from contextlib import suppress
import re
import time
from typing import Annotated, Any
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, Request, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.service.auth import (
    AuthenticationError,
    BearerTokenAuthenticator,
)
from short_term_memory.service.memory_service import (
    MemoryReadUnavailableError,
    RetryableWriteError,
)
from short_term_memory.service.metrics import ApiMetrics
from short_term_memory.service.schemas import (
    MemoryReadRequest,
    MemoryReadResponse,
    MemoryRecallRequest,
    MemoryRecallResponse,
    MemoryWriteRequest,
    MemoryWriteResponse,
)
from short_term_memory.storage.async_redis_memory_store import EventConflictError
from short_term_memory.storage.journal_store import JournalConflictError


_BUSINESS_PATHS = frozenset(
    {"/v1/memories/write", "/v1/memories/read", "/v1/memories/recall"}
)
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: str
    request_id: str


class RequestContractError(ValueError):
    """A setting-dependent request constraint was violated."""


class HttpControlMiddleware:
    """Enforce request limits before FastAPI reads or parses a business body."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        concurrency_limit: int,
        metrics: ApiMetrics,
        authenticator: BearerTokenAuthenticator,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.capacity = anyio.CapacityLimiter(concurrency_limit)
        self.metrics = metrics
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET"))
        request_id = self._request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        response_status = 500
        response_started = False
        acquired = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, response_status
            if message["type"] == "http.response.start":
                response_started = True
                response_status = int(message["status"])
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        with self.metrics.track_in_flight():
            try:
                controlled_receive = receive
                if path in _BUSINESS_PATHS:
                    try:
                        self.authenticator.verify(self._authorization(scope))
                    except AuthenticationError:
                        response_status = status.HTTP_401_UNAUTHORIZED
                        await self._send_error(
                            send,
                            response_status,
                            "unauthorized",
                            request_id,
                            extra_headers=((b"www-authenticate", b"Bearer"),),
                        )
                        return

                    try:
                        self.capacity.acquire_nowait()
                        acquired = True
                    except anyio.WouldBlock:
                        response_status = status.HTTP_429_TOO_MANY_REQUESTS
                        await self._send_error(
                            send,
                            response_status,
                            "overloaded",
                            request_id,
                            extra_headers=((b"retry-after", b"1"),),
                        )
                        return

                    buffered = await self._bounded_body(scope, receive)
                    if buffered is None:
                        response_status = status.HTTP_413_CONTENT_TOO_LARGE
                        await self._send_error(
                            send,
                            response_status,
                            "request_too_large",
                            request_id,
                        )
                        return
                    controlled_receive = self._replay(buffered)

                try:
                    await self.app(scope, controlled_receive, send_with_request_id)
                except Exception:
                    if response_started:
                        raise
                    response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
                    await self._send_error(
                        send,
                        response_status,
                        "internal_error",
                        request_id,
                    )
            finally:
                if acquired:
                    self.capacity.release()
                self.metrics.observe_http(
                    path, method, response_status, time.perf_counter() - started
                )

    async def _bounded_body(
        self, scope: Scope, receive: Receive
    ) -> list[Message] | None:
        content_length = self._content_length(scope)
        if content_length is None:
            pass
        elif content_length < 0 or content_length > self.max_body_bytes:
            return None

        total = 0
        messages: list[Message] = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                return messages
            total += len(message.get("body", b""))
            if total > self.max_body_bytes:
                return None
            if not message.get("more_body", False):
                return messages

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                with suppress(ValueError):
                    return int(value)
                return -1
        return None

    @staticmethod
    def _authorization(scope: Scope) -> str | None:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return None
        with suppress(UnicodeDecodeError):
            return values[0].decode("ascii")
        return None

    @staticmethod
    def _replay(messages: list[Message]) -> Receive:
        iterator = iter(messages)

        async def replay() -> Message:
            return next(iterator, {"type": "http.disconnect"})

        return replay

    @staticmethod
    def _request_id(scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() != b"x-request-id":
                continue
            with suppress(UnicodeDecodeError):
                candidate = value.decode("ascii")
                if _REQUEST_ID_PATTERN.fullmatch(candidate):
                    return candidate
            break
        return uuid4().hex

    @staticmethod
    async def _send_error(
        send: Send,
        status_code: int,
        code: str,
        request_id: str,
        *,
        extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        body = (
            ErrorResponse(error=code, request_id=request_id).model_dump_json().encode()
        )
        headers = (
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"x-request-id", request_id.encode("ascii")),
            *extra_headers,
        )
        await send(
            {"type": "http.response.start", "status": status_code, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})


_ERROR_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Authentication failed."},
    409: {"model": ErrorResponse, "description": "Event ID conflict."},
    413: {"model": ErrorResponse, "description": "Request body too large."},
    422: {"model": ErrorResponse, "description": "Request validation failed."},
    429: {"model": ErrorResponse, "description": "Concurrency limit reached."},
    503: {"model": ErrorResponse, "description": "Memory service unavailable."},
    500: {"model": ErrorResponse, "description": "Internal service error."},
}


def create_app(
    runtime_factory: Callable[[], Any],
    *,
    settings: ShortTermMemorySettings | None = None,
    metrics: ApiMetrics | None = None,
    lifespan: Any | None = None,
) -> FastAPI:
    """Build an authenticated memory API around one injected MemoryService."""

    effective_settings = settings or ShortTermMemorySettings()
    api_metrics = metrics or ApiMetrics()
    authenticator = BearerTokenAuthenticator(
        auth_token=effective_settings.api.auth_token,
        environment=effective_settings.environment,
    )
    memory_service = runtime_factory()
    if isinstance(memory_service, Awaitable):
        raise TypeError("runtime_factory must return a ready MemoryService")

    app = FastAPI(
        title="Short-Term Memory API", version="1.0.0", lifespan=lifespan
    )
    app.state.memory_service = memory_service
    app.state.metrics = api_metrics
    app.add_middleware(
        HttpControlMiddleware,
        max_body_bytes=effective_settings.api.max_body_bytes,
        concurrency_limit=effective_settings.api.concurrency_limit,
        metrics=api_metrics,
        authenticator=authenticator,
    )

    bearer_scheme = HTTPBearer(auto_error=False, scheme_name="HTTPBearer")

    async def authenticate(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Security(bearer_scheme)
        ],
    ) -> None:
        authorization = (
            None
            if credentials is None
            else f"{credentials.scheme} {credentials.credentials}"
        )
        authenticator.verify(authorization)

    def error_response(
        request: Request,
        status_code: int,
        code: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        return JSONResponse(
            status_code=status_code,
            content={"error": code, "request_id": request_id},
            headers=headers,
        )

    @app.exception_handler(AuthenticationError)
    async def authentication_error(
        request: Request, _error: AuthenticationError
    ) -> JSONResponse:
        return error_response(
            request,
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            request, status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error"
        )

    @app.exception_handler(RequestContractError)
    async def contract_error(
        request: Request, _error: RequestContractError
    ) -> JSONResponse:
        return error_response(
            request, status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error"
        )

    async def conflict_error(request: Request, _error: Exception) -> JSONResponse:
        return error_response(request, status.HTTP_409_CONFLICT, "event_id_conflict")

    app.add_exception_handler(EventConflictError, conflict_error)
    app.add_exception_handler(JournalConflictError, conflict_error)

    async def unavailable_error(request: Request, _error: Exception) -> JSONResponse:
        return error_response(
            request, status.HTTP_503_SERVICE_UNAVAILABLE, "service_unavailable"
        )

    for error_type in (
        RetryableWriteError,
        MemoryReadUnavailableError,
        RedisError,
        OSError,
        TimeoutError,
        ConnectionError,
    ):
        app.add_exception_handler(error_type, unavailable_error)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _error: Exception) -> JSONResponse:
        return error_response(
            request, status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"
        )

    @app.get("/health", include_in_schema=True)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=True)
    async def ready() -> JSONResponse:
        runtime = getattr(app.state, "service_runtime", None)
        components = {"redis": False, "headroom": False}
        if runtime is not None:
            try:
                result = await runtime.readiness()
                components = {
                    "redis": bool(result.get("redis", False)),
                    "headroom": bool(result.get("headroom", False)),
                }
            except Exception:
                pass
        is_ready = all(components.values())
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK
                if is_ready
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={
                "status": "ready" if is_ready else "not_ready",
                "components": components,
            },
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(
            content=api_metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    def observe_phases(timing: BaseModel) -> None:
        for stage, duration_ms in timing.model_dump().items():
            api_metrics.observe_phase(stage, float(duration_ms))

    @app.post(
        "/v1/memories/write",
        response_model=MemoryWriteResponse,
        responses=_ERROR_RESPONSES,
        dependencies=[Depends(authenticate)],
    )
    async def write_memory(request: Request, body: MemoryWriteRequest) -> Any:
        if len(body.events) > effective_settings.api.write_max_batch_events:
            raise RequestContractError("write batch exceeds configured limit")
        response = await app.state.memory_service.write(body, request.state.request_id)
        observe_phases(response.timing_ms)
        return response

    @app.post(
        "/v1/memories/read",
        response_model=MemoryReadResponse,
        responses=_ERROR_RESPONSES,
        dependencies=[Depends(authenticate)],
    )
    async def read_memory(request: Request, body: MemoryReadRequest) -> Any:
        response = await app.state.memory_service.read(body, request.state.request_id)
        observe_phases(response.timing_ms)
        return response

    @app.post(
        "/v1/memories/recall",
        response_model=MemoryRecallResponse,
        responses=_ERROR_RESPONSES,
        dependencies=[Depends(authenticate)],
    )
    async def recall_memory(request: Request, body: MemoryRecallRequest) -> Any:
        return await app.state.memory_service.recall(body, request.state.request_id)

    return app
