from datetime import datetime, timezone

from dream.curators.registry import CuratorRegistry
from dream.events import TaskCompletedEvent
from dream.scheduler import DreamScheduler
from dream.scope import ScopeIds


def _event(event_id: str, *, interrupted: bool = False) -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ScopeIds("acme", "assistant", "alice"),
        completed_at="2026-07-15T10:00:00+08:00",
        interrupted=interrupted,
        tool_iterations=12,
        transcript=({"role": "user", "content": "I prefer concise answers"},),
        final_response="Understood." if not interrupted else "",
        source_refs=(),
    )


def test_scheduler_ignores_interrupted_conversations() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    scheduler.enqueue(_event("evt-interrupted", interrupted=True))
    scheduler.enqueue(_event("evt-completed"))
    assert scheduler.pending_event_ids() == ("evt-completed",)


def test_scheduler_pops_only_the_requested_scope() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    alice = _event("evt-alice")
    bob = TaskCompletedEvent(
        **{
            **alice.__dict__,
            "event_id": "evt-bob",
            "scope": ScopeIds("acme", "assistant", "bob"),
        }
    )
    scheduler.enqueue(alice)
    scheduler.enqueue(bob)

    assert scheduler.pop_pending(bob.scope) == bob
    assert scheduler.pending_event_ids() == ("evt-alice",)


def test_scheduler_does_not_enqueue_the_same_pending_event_twice() -> None:
    scheduler = DreamScheduler(review_threshold=10)
    completed = _event("evt-completed")

    scheduler.enqueue_unless_pending(completed)
    scheduler.enqueue_unless_pending(completed)

    assert scheduler.pending_event_ids() == ("evt-completed",)


def test_curator_registry_runs_only_due_curators() -> None:
    class RecordingCurator:
        name = "recording"

        def __init__(self) -> None:
            self.ran = False

        def should_run(self, now: datetime) -> bool:
            return True

        def run(self) -> str:
            self.ran = True
            return "ok"

    curator = RecordingCurator()
    results = CuratorRegistry([curator]).run_due(datetime.now(timezone.utc))
    assert curator.ran is True
    assert results == {"recording": "ok"}
