from pathlib import Path

import pytest

from dream.closed_loop import (
    ClosedLoopCoordinator,
    ClosedLoopError,
    TaskStartBlocked,
)
from dream.events import TaskCompletedEvent
from dream.publication import PublicationStatus, PublicationTransitionError
from dream.scope import ScopeIds
from dream.service import DreamService
from dream.writeback import DeterministicWritebackBackend


IDS = ScopeIds("dream-lab", "enterprise-colleague", "python-beginner")


def event(event_id: str, ids: ScopeIds = IDS) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ids,
        completed_at="2026-07-17T10:00:00+08:00",
        interrupted=False,
        tool_iterations=10,
        transcript=(
            {"role": "user", "content": "I prefer concise answers"},
            {"role": "assistant", "content": "Always verify before risky action"},
        ),
        final_response="Verified before applying the change.",
        source_refs=(),
    )


def coordinator(tmp_path: Path, *, backend=None):
    service = DreamService(tmp_path)
    closed_loop = ClosedLoopCoordinator(
        service,
        writeback_backend=backend or DeterministicWritebackBackend(),
    )
    return closed_loop, service


def activate(closed_loop: ClosedLoopCoordinator, version: int):
    closed_loop.approve(IDS, version)
    closed_loop.confirm_writeback(
        IDS,
        version,
        character_written=True,
        user_written=True,
    )
    return closed_loop.activate(IDS, version)


def test_next_task_waits_for_latest_event_to_be_active(tmp_path: Path) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))

    with pytest.raises(TaskStartBlocked, match="evt-1"):
        closed_loop.assert_task_can_start(IDS)

    candidate = closed_loop.dream(IDS)
    active = activate(closed_loop, candidate.version)

    assert active.processed_through_event_id == "evt-1"
    closed_loop.assert_task_can_start(IDS)


def test_failed_writeback_restores_previous_snapshot(tmp_path: Path) -> None:
    class FailingBackend(DeterministicWritebackBackend):
        def render_user_persona(self, user_profile: str, limit: int) -> str:
            raise RuntimeError("provider unavailable")

    closed_loop, service = coordinator(tmp_path, backend=FailingBackend())
    service.ingest_conversation(event("evt-failed"))

    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    latest = closed_loop.status(IDS)["latest"]
    assert latest.status is PublicationStatus.FAILED
    assert closed_loop.status(IDS)["active"] is None
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-failed",)


def test_failed_candidate_can_be_retried_from_its_source_event(tmp_path: Path) -> None:
    class FailOnceBackend(DeterministicWritebackBackend):
        def __init__(self) -> None:
            self.failed = False

        def render_user_persona(self, user_profile: str, limit: int) -> str:
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary provider failure")
            return super().render_user_persona(user_profile, limit)

    backend = FailOnceBackend()
    closed_loop, service = coordinator(tmp_path, backend=backend)
    service.ingest_conversation(event("evt-retry"))
    with pytest.raises(ClosedLoopError):
        closed_loop.dream(IDS)

    candidate = closed_loop.dream(IDS)

    assert candidate.status is PublicationStatus.READY_FOR_REVIEW
    assert candidate.processed_through_event_id == "evt-retry"


def test_identical_writeback_hashes_do_not_require_repeated_paste(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))
    first = closed_loop.dream(IDS)
    activate(closed_loop, first.version)
    service.ingest_conversation(event("evt-2"))
    second = closed_loop.dream(IDS)
    closed_loop.approve(IDS, second.version)

    confirmed = closed_loop.confirm_writeback(
        IDS,
        second.version,
        character_written=False,
        user_written=False,
    )

    assert confirmed.character_definition_written is True
    assert confirmed.user_persona_written is True
    assert closed_loop.activate(IDS, second.version).status is PublicationStatus.ACTIVE


def test_rollback_restores_a_previous_active_version(tmp_path: Path) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-1"))
    first = closed_loop.dream(IDS)
    activate(closed_loop, first.version)
    service.ingest_conversation(event("evt-2"))
    second = closed_loop.dream(IDS)
    activate(closed_loop, second.version)

    restored = closed_loop.rollback(IDS, first.version)

    assert restored.version == first.version
    assert closed_loop.status(IDS)["active"].version == first.version


def test_reject_restores_input_state_and_requeues_source_event(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-rejected"))
    candidate = closed_loop.dream(IDS)

    rejected = closed_loop.reject(IDS, candidate.version)

    assert rejected.status is PublicationStatus.FAILED
    assert closed_loop.publications(IDS).pending_event_ids() == ("evt-rejected",)
    assert service.scheduler.pending_event_ids() == ("evt-rejected",)


def test_rejecting_active_version_does_not_restore_its_before_snapshot(
    tmp_path: Path,
) -> None:
    closed_loop, service = coordinator(tmp_path)
    service.ingest_conversation(event("evt-active"))
    candidate = closed_loop.dream(IDS)
    active = activate(closed_loop, candidate.version)
    profile_before = service.start_context(IDS)["user_profile"]

    with pytest.raises(PublicationTransitionError):
        closed_loop.reject(IDS, active.version)

    assert service.start_context(IDS)["user_profile"] == profile_before
    assert closed_loop.status(IDS)["active"].version == active.version
