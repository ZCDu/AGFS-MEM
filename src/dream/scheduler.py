"""Conversation review queue and Hermes-style iteration bookkeeping."""

from collections import defaultdict, deque

from dream.events import TaskCompletedEvent
from dream.scope import ScopeIds


class DreamScheduler:
    def __init__(self, review_threshold: int = 10) -> None:
        if review_threshold < 1:
            raise ValueError("review_threshold must be positive")
        self.review_threshold = review_threshold
        self._pending: deque[TaskCompletedEvent] = deque()
        self._iterations: dict[ScopeIds, int] = defaultdict(int)

    def enqueue(self, event: TaskCompletedEvent) -> None:
        if event.interrupted or not event.final_response:
            return
        self._pending.append(event)
        self._iterations[event.scope] += max(0, event.tool_iterations)

    def enqueue_unless_pending(self, event: TaskCompletedEvent) -> None:
        if event.event_id in self.pending_event_ids():
            return
        self.enqueue(event)

    def pending_event_ids(self) -> tuple[str, ...]:
        return tuple(event.event_id for event in self._pending)

    def pop_pending(self, scope: ScopeIds | None = None) -> TaskCompletedEvent | None:
        if scope is None:
            if not self._pending:
                return None
            return self._pending.popleft()
        for index, event in enumerate(self._pending):
            if event.scope == scope:
                del self._pending[index]
                return event
        return None

    def scope_is_ready(self, scope: ScopeIds) -> bool:
        return self._iterations[scope] >= self.review_threshold

    def mark_review_accepted(self, scope: ScopeIds) -> None:
        self._iterations[scope] = 0
