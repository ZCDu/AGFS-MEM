"""Coordinate dream candidates, manual writeback, activation, and rollback."""

from dream.artifacts import AtomicArtifactStore
from dream.publication import (
    PublicationStatus,
    PublicationStore,
    PublicationTransitionError,
    PublicationVersion,
)
from dream.reports import DreamReportStore
from dream.scope import ScopeIds, resolve_scope
from dream.service import DreamService
from dream.snapshots import SnapshotStore
from dream.writeback import WritebackBackend, WritebackService


class ClosedLoopError(RuntimeError):
    """A candidate failed without exposing provider details."""


class TaskStartBlocked(RuntimeError):
    def __init__(self, latest_event_id: str, active_event_id: str) -> None:
        self.latest_event_id = latest_event_id
        self.active_event_id = active_event_id
        super().__init__(
            f"latest event {latest_event_id} is not active; active event is "
            f"{active_event_id or 'none'}"
        )


class ClosedLoopCoordinator:
    def __init__(
        self,
        service: DreamService,
        *,
        writeback_backend: WritebackBackend,
        character_limit: int = 3200,
        user_persona_limit: int = 1200,
    ) -> None:
        self.service = service
        self.writeback_backend = writeback_backend
        self.character_limit = character_limit
        self.user_persona_limit = user_persona_limit

    def _paths(self, ids: ScopeIds):
        return resolve_scope(self.service.home, ids)

    def publications(self, ids: ScopeIds) -> PublicationStore:
        return PublicationStore(self._paths(ids))

    def _snapshots(self, ids: ScopeIds) -> SnapshotStore:
        paths = self._paths(ids)
        return SnapshotStore(paths, AtomicArtifactStore(paths.agent_root))

    def _writebacks(self, ids: ScopeIds) -> WritebackService:
        return WritebackService(
            self._paths(ids),
            backend=self.writeback_backend,
            character_limit=self.character_limit,
            user_persona_limit=self.user_persona_limit,
        )

    def dream(self, ids: ScopeIds) -> PublicationVersion:
        publications = self.publications(ids)
        pending = publications.pending_event_ids()
        if not pending:
            raise ValueError("no completed events are waiting for a dream")
        snapshots = self._snapshots(ids)
        before = snapshots.create(ids)
        version = publications.begin(pending, pending[-1], before.snapshot_id)
        try:
            version = publications.mark_dreaming(version.version)
            reviews = self.service.run_pending(ids)
            if not reviews or any(run["status"] != "success" for run in reviews):
                raise RuntimeError("background review failed")
            self.service.run_curators(ids)
            writeback = self._writebacks(ids).generate()
            after = snapshots.create(ids)
            ready = publications.mark_ready_for_review(
                version.version,
                after.snapshot_id,
                writeback.character.sha256,
                writeback.user_persona.sha256,
            )
            self._report(ids, ready, "candidate ready")
            return ready
        except Exception as exc:
            snapshots.restore(before.snapshot_id, ids)
            for event_id in pending:
                self.service.review_progress.invalidate(event_id)
            self.service.recover_pending()
            failed = publications.fail(version.version, type(exc).__name__)
            self._report(ids, failed, "candidate failed")
            raise ClosedLoopError("dream candidate failed") from exc

    def approve(self, ids: ScopeIds, version: int) -> PublicationVersion:
        return self.publications(ids).approve(version)

    def confirm_writeback(
        self,
        ids: ScopeIds,
        version: int,
        *,
        character_written: bool,
        user_written: bool,
    ) -> PublicationVersion:
        publications = self.publications(ids)
        candidate = publications.get(version)
        active = publications.active()
        if active is not None:
            character_written = character_written or (
                candidate.character_definition_sha256
                == active.character_definition_sha256
            )
            user_written = user_written or (
                candidate.user_persona_sha256 == active.user_persona_sha256
            )
        return publications.confirm_writeback(
            version,
            character_written=character_written,
            user_written=user_written,
        )

    def activate(self, ids: ScopeIds, version: int) -> PublicationVersion:
        active = self.publications(ids).activate(version)
        self._report(ids, active, "version active")
        return active

    def reject(self, ids: ScopeIds, version: int) -> PublicationVersion:
        publications = self.publications(ids)
        candidate = publications.get(version)
        if candidate.status not in {
            PublicationStatus.READY_FOR_REVIEW,
            PublicationStatus.READY_FOR_WRITEBACK,
        }:
            raise PublicationTransitionError(
                f"cannot reject publication in {candidate.status.value} state"
            )
        self._snapshots(ids).restore(candidate.before_snapshot_id, ids)
        for event_id in candidate.source_event_ids:
            self.service.review_progress.invalidate(event_id)
        self.service.recover_pending()
        return publications.fail(version, "rejected")

    def rollback(self, ids: ScopeIds, version: int) -> PublicationVersion:
        publications = self.publications(ids)
        selected = publications.get(version)
        if not selected.after_snapshot_id:
            raise ValueError("rollback target has no completed snapshot")
        self._snapshots(ids).restore(selected.after_snapshot_id, ids)
        restored = publications.restore_active(version)
        self._report(ids, restored, "active version rolled back")
        return restored

    def status(self, ids: ScopeIds) -> dict[str, PublicationVersion | None]:
        publications = self.publications(ids)
        return {"latest": publications.latest(), "active": publications.active()}

    def assert_task_can_start(self, ids: ScopeIds) -> None:
        events = [
            event for event in self.service.ledger.read_all() if event.scope == ids
        ]
        if not events:
            return
        latest = events[-1].event_id
        active = self.publications(ids).active()
        active_event = active.processed_through_event_id if active else ""
        if latest != active_event:
            raise TaskStartBlocked(latest, active_event)

    def _report(self, ids: ScopeIds, version: PublicationVersion, summary: str) -> None:
        DreamReportStore(self._paths(ids)).write(
            {
                "run_id": f"publication-{version.version:06d}-{version.status.value}",
                "curator": "closed_loop",
                "status": version.status.value,
                "version": version.version,
                "source_event_ids": list(version.source_event_ids),
                "before_snapshot_id": version.before_snapshot_id,
                "after_snapshot_id": version.after_snapshot_id,
                "character_definition_sha256": version.character_definition_sha256,
                "user_persona_sha256": version.user_persona_sha256,
                "fallback_version": version.fallback_version,
                "summary": summary,
            }
        )
