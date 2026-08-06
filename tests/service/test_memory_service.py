from datetime import datetime, timedelta, timezone

import pytest

from short_term_memory.compression.generations import GenerationAssembler
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.models import CompressionGeneration, EventReservation
from short_term_memory.service.memory_service import MemoryService, RetryableWriteError
from tests.factories import envelope, memory_event, read_request, write_request


class RecordingStore:
    def __init__(self, calls):
        self.calls = calls
        self.events = []
        self.envelope = None
        self.reservations = {}
        self.fail_next_commit = False

    async def reserve_event(self, user_id, session_id, event_id, digest):
        self.calls.append("reserve")
        if event_id in self.reservations:
            return self.reservations[event_id]
        reservation = EventReservation(sequence=len(self.reservations) + 1, state="reserved")
        self.reservations[event_id] = reservation
        return reservation

    async def commit_event(self, user_id, session_id, event):
        self.calls.append("redis_commit")
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise OSError("redis unavailable")
        if event not in self.events:
            self.events.append(event)
            return "committed"
        return "duplicate"

    async def read_envelope(self, user_id, session_id):
        return self.envelope

    async def read_recent_originals(self, user_id, session_id, history_turns):
        return tuple(self.events[-history_turns:])

    async def read_originals_after(self, user_id, session_id, sequence):
        return tuple(event for event in self.events if event.sequence > sequence)

    def seed_envelope(self, value):
        self.envelope = value


class RecordingJournals:
    def __init__(self, calls):
        self.calls = calls
        self.events = []

    def append_event(self, user_id, session_id, event):
        self.calls.append("journal_fsync")
        if not any(existing.event_id == event.event_id for existing in self.events):
            self.events.append(event)

    def append_count(self, event_id):
        return sum(event.event_id == event_id for event in self.events)

    def read_original_range(self, user_id, session_id, from_sequence, through_sequence):
        return tuple(
            event
            for event in self.events
            if from_sequence <= event.sequence <= through_sequence
        )


class RecordingQueue:
    def __init__(self, calls):
        self.calls = calls
        self.jobs = []

    async def enqueue(self, job):
        self.calls.append("enqueue")
        self.jobs.append(job)
        return "ready"


class RecordingPolicy(HeadroomPolicy):
    def __init__(self, calls):
        super().__init__(context_window_tokens=1, trigger_ratio=0.65, max_messages=1, max_session_seconds=1)
        self.calls = calls

    def should_compress(self, **kwargs):
        self.calls.append("policy")
        return True


@pytest.fixture
def service():
    calls = []
    store = RecordingStore(calls)
    journals = RecordingJournals(calls)
    return MemoryService(
        store=store,
        journals=journals,
        assembler=GenerationAssembler(max_segments=8),
        compression_queue=RecordingQueue(calls),
        policy=RecordingPolicy(calls),
        scope_factory=OptimizationScopeFactory("secret"),
        settings=ShortTermMemorySettings(),
        headroom_proxy_url="http://headroom:8787/v1",
        clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
        token_estimator=lambda events: len(events),
    )


@pytest.mark.asyncio
async def test_write_reserves_journals_commits_then_queues(service):
    response = await service.write(write_request("event-1", "original"), "req-1")

    assert service.store.calls == ["reserve", "journal_fsync", "redis_commit", "policy", "enqueue"]
    assert response.accepted is True
    assert response.sequence_from == response.sequence_through == 1
    assert service.journals.events[0].content == "original"


@pytest.mark.asyncio
async def test_retry_after_commit_failure_repairs_without_duplicate_journal(service):
    service.store.fail_next_commit = True

    with pytest.raises(RetryableWriteError):
        await service.write(write_request("event-1", "original"), "req-1")

    response = await service.write(write_request("event-1", "original"), "req-2")

    assert response.accepted is True
    assert service.journals.append_count("event-1") == 1


@pytest.mark.asyncio
async def test_read_returns_proxy_scope_without_calling_deepseek(service):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=({"role": "system", "content": "HR-MARKER"},),
        tokens_before=2,
        tokens_after=1,
        created_at=now.isoformat(),
        ccr_expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    service.store.seed_envelope(envelope(through=1, generations=[generation]))
    service.store.events.append(memory_event())

    response = await service.read(read_request(), "req-2")

    assert any("HR-MARKER" in str(message.content) for message in response.messages)
    assert response.headroom.proxy_url == "http://headroom:8787/v1"
    assert set(response.headroom.scope_headers) == {
        "x-headroom-user-id", "x-headroom-session-id", "x-headroom-project-id"
    }
    assert not hasattr(service, "deepseek_client")


@pytest.mark.asyncio
async def test_read_recovers_recent_originals_from_journal_when_redis_is_empty(service):
    recovered = memory_event(sequence=1, event_id="event-1", content="journal-original")
    service.journals.events.append(recovered)

    response = await service.read(read_request(), "req-2")

    assert response.memory.source == "journal_rebuild"
    assert response.memory.latest_sequence == 1
    assert response.messages[-1].content == "journal-original"
    assert service.store.events == [recovered]


@pytest.mark.asyncio
async def test_read_omits_effective_config_when_not_requested(service):
    request = read_request().model_copy(update={"include_effective_config": False})

    response = await service.read(request, "req-2")

    assert response.effective_config is None
