"""HTTP adapter between short-term memory and DREAM."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response, status
import httpx
from pydantic import BaseModel, ConfigDict, Field

from dream.config import build_curator_backend, build_review_backend, load_settings
from dream.events import TaskCompletedEvent
from dream.scope import ScopeIds
from dream.service import DreamService
from dream.source_sync import InternshipSourceSync
from dream.sources.internship import InternshipSourceClient
from dream.sources.manual import ManualSourceError


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    agent_id: str
    user_id: str

    def to_ids(self) -> ScopeIds:
        return ScopeIds(self.tenant_id, self.agent_id, self.user_id)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class ConversationRequest(ScopeRequest):
    event_id: str
    conversation_id: str
    completed_at: str
    interrupted: bool = False
    tool_iterations: int = Field(default=10, ge=0)
    headroom_summary: str = ""
    messages: list[ConversationMessage]
    final_response: str

    def to_event(self) -> TaskCompletedEvent:
        transcript: list[dict[str, object]] = [
            message.model_dump() for message in self.messages
        ]
        if self.headroom_summary:
            transcript.append(
                {"role": "headroom_summary", "content": self.headroom_summary}
            )
        return TaskCompletedEvent(
            event_id=self.event_id,
            task_id=self.conversation_id,
            scope=self.to_ids(),
            completed_at=self.completed_at,
            interrupted=self.interrupted,
            tool_iterations=self.tool_iterations,
            transcript=tuple(transcript),
            final_response=self.final_response,
            source_refs=(),
        )


logger = logging.getLogger(__name__)


def create_app(
    home: Path,
    worker_interval_seconds: float = 60.0,
    *,
    env_file: Path | None = None,
    client_factory: Callable[..., object] | None = None,
    source_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    resolved_env_file = env_file or Path(
        os.environ.get("DREAM_ENV_FILE", ".env")
    ).expanduser()
    settings = load_settings(resolved_env_file)
    backend = build_review_backend(settings, client_factory=client_factory)
    semantic_curator_backend = build_curator_backend(
        settings, client_factory=client_factory
    )
    service = DreamService(
        home,
        backend=backend,
        semantic_curator_backend=semantic_curator_backend,
    )
    source_sync: InternshipSourceSync | None = None
    if settings.internship_source.enabled:
        source_sync = InternshipSourceSync(
            service,
            settings.internship_source,
            client=InternshipSourceClient(
                settings.internship_source,
                transport=source_transport,
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()

        async def dream_worker() -> None:
            while not stop.is_set():
                if source_sync is not None:
                    try:
                        source_result = await asyncio.to_thread(
                            source_sync.sync_if_due, datetime.now(timezone.utc)
                        )
                        if source_result.status == "error":
                            logger.warning(
                                "DREAM source sync failed: %s",
                                "; ".join(source_result.errors),
                            )
                    except Exception:
                        logger.exception("DREAM source sync iteration failed")
                try:
                    await asyncio.to_thread(service.run_pending)
                    await asyncio.to_thread(
                        service.run_due_curators, datetime.now(timezone.utc)
                    )
                except Exception:
                    logger.exception("DREAM background worker iteration failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=worker_interval_seconds)
                except TimeoutError:
                    continue

        worker = asyncio.create_task(dream_worker())
        try:
            yield
        finally:
            stop.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    application = FastAPI(title="DREAM", version="0.1.0", lifespan=lifespan)
    application.state.dream_service = service
    application.state.internship_source_sync = source_sync

    @application.post("/v1/tasks/start")
    def start_task(scope: ScopeRequest) -> dict[str, object]:
        try:
            return service.start_context(scope.to_ids())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post(
        "/v1/dream/conversations", status_code=status.HTTP_202_ACCEPTED
    )
    def ingest_conversation(payload: ConversationRequest) -> dict[str, object]:
        try:
            service.ingest_conversation(payload.to_event())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"event_id": payload.event_id, "status": "queued"}

    @application.post("/v1/validation/import")
    async def import_manual_ndjson(request: Request) -> dict[str, int]:
        try:
            text = (await request.body()).decode("utf-8")
            return service.import_manual_ndjson(text)
        except (ManualSourceError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid manual completed-task NDJSON",
            ) from exc

    @application.post("/v1/dream/run-pending")
    def run_pending() -> dict[str, object]:
        return {"runs": service.run_pending()}

    @application.post("/v1/dream/run-curators")
    def run_curators(scope: ScopeRequest) -> dict[str, object]:
        reports = service.run_curators(scope.to_ids())
        return {
            name: {
                "run_id": report.run_id,
                "status": report.status,
                "changed": report.changed,
                "archived": report.archived,
                "rollback_snapshot_id": report.rollback_snapshot_id,
            }
            for name, report in reports.items()
        }

    @application.post("/v1/dream/run-due-curators")
    def run_due_curators() -> dict[str, object]:
        results = service.run_due_curators(datetime.now(timezone.utc))
        return {
            "scopes": {
                scope_key: {
                    name: {
                        "run_id": report.run_id,
                        "status": report.status,
                        "changed": report.changed,
                        "archived": report.archived,
                    }
                    for name, report in reports.items()
                }
                for scope_key, reports in results.items()
            }
        }

    @application.post("/v1/dream/rollback/{snapshot_id}")
    def rollback(snapshot_id: str, scope: ScopeRequest) -> dict[str, str]:
        try:
            service.rollback(scope.to_ids(), snapshot_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"snapshot_id": snapshot_id, "status": "restored"}

    @application.get("/v1/dream/reports/{run_id}")
    def read_report(
        run_id: str, tenant_id: str, agent_id: str, user_id: str
    ) -> Response:
        report = service.read_report(
            ScopeIds(tenant_id, agent_id, user_id), run_id
        )
        if not report:
            raise HTTPException(status_code=404, detail="report not found")
        return Response(content=report, media_type="application/json")

    return application


_default_env_file = Path(os.environ.get("DREAM_ENV_FILE", ".env")).expanduser()
_default_settings = load_settings(_default_env_file)
app = create_app(Path(_default_settings.home).expanduser(), env_file=_default_env_file)
