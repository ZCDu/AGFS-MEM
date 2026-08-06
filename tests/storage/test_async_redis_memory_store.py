from hashlib import sha256

import pytest

from short_term_memory.models import MemoryEvent
from short_term_memory.storage.async_redis_memory_store import (
    AsyncRedisMemoryStore,
    EventConflictError,
)
from tests.factories import envelope, memory_event
from tests.storage.fake_redis import AsyncFakeRedis


@pytest.fixture
def redis() -> AsyncFakeRedis:
    return AsyncFakeRedis()


@pytest.fixture
def memory_store(redis: AsyncFakeRedis) -> AsyncRedisMemoryStore:
    return AsyncRedisMemoryStore(redis)


@pytest.mark.asyncio
async def test_reserve_retry_and_conflict(memory_store: AsyncRedisMemoryStore) -> None:
    first = await memory_store.reserve_event("u", "s", "e", "a" * 64)
    retry = await memory_store.reserve_event("u", "s", "e", "a" * 64)

    assert first.sequence == retry.sequence == 1
    assert first.state == "reserved"
    assert retry.state == "pending"
    with pytest.raises(EventConflictError):
        await memory_store.reserve_event("u", "s", "e", "b" * 64)


@pytest.mark.asyncio
async def test_commit_makes_event_visible_once(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(event_id="e")
    reservation = await memory_store.reserve_event("u", "s", "e", event.sha256)
    event = event.model_copy(update={"sequence": reservation.sequence})

    assert await memory_store.commit_event("u", "s", event) == "committed"
    assert await memory_store.commit_event("u", "s", event) == "duplicate"
    assert await memory_store.read_recent_originals("u", "s", 10) == (event,)


@pytest.mark.asyncio
async def test_read_originals_after_returns_only_later_original_events(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    first = memory_event(sequence=1, event_id="first", content="first")
    second = memory_event(sequence=2, event_id="second", content="second")
    for event in (first, second):
        await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
        await memory_store.commit_event("u", "s", event)

    assert await memory_store.read_originals_after("u", "s", 1) == (second,)


@pytest.mark.asyncio
async def test_summary_cas_rejects_stale_worker(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.compare_and_set_envelope("u", "s", 0, envelope(version=1))
    assert not await memory_store.compare_and_set_envelope("u", "s", 0, envelope(version=2))
    assert (await memory_store.read_envelope("u", "s")).version == 1


@pytest.mark.asyncio
async def test_reservation_commit_and_summary_refresh_consistent_ttls(
    redis: AsyncFakeRedis, memory_store: AsyncRedisMemoryStore
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
    await memory_store.compare_and_set_envelope("u", "s", 0, envelope())
    await memory_store.commit_event("u", "s", event)

    prefix = "dream:session:u:s"
    expected = {
        f"{prefix}:sequence",
        f"{prefix}:messages",
        f"{prefix}:summary",
        f"{prefix}:event:event",
    }
    assert redis.ttls == {
        key: 43_200 for key in expected
    }


@pytest.mark.asyncio
async def test_compression_lease_is_exclusive_and_token_scoped(
    redis: AsyncFakeRedis,
    memory_store: AsyncRedisMemoryStore,
) -> None:
    assert await memory_store.acquire_compression_lease("u", "s", "one")
    assert not await memory_store.acquire_compression_lease("u", "s", "two")
    assert not await memory_store.release_compression_lease("u", "s", "two")
    assert await memory_store.release_compression_lease("u", "s", "one")
    assert await memory_store.acquire_compression_lease("u", "s", "two")
    redis.expire_now("dream:session:u:s:compression-lock")
    assert await memory_store.acquire_compression_lease("u", "s", "three")


@pytest.mark.asyncio
async def test_commit_rejects_different_digest_for_reserved_event(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event", content="original")
    conflicting_event = memory_event(sequence=1, event_id="event", content="changed")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)

    with pytest.raises(EventConflictError, match="digest"):
        await memory_store.commit_event("u", "s", conflicting_event)

    assert await memory_store.read_recent_originals("u", "s", 10) == ()


@pytest.mark.asyncio
async def test_commit_rejects_wrong_sequence_for_reserved_event(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)

    with pytest.raises(ValueError, match="sequence"):
        await memory_store.commit_event(
            "u", "s", event.model_copy(update={"sequence": 2})
        )

    assert await memory_store.read_recent_originals("u", "s", 10) == ()


@pytest.mark.asyncio
async def test_duplicate_rechecks_reservation_digest_and_sequence(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    event = memory_event(sequence=1, event_id="event")
    await memory_store.reserve_event("u", "s", event.event_id, event.sha256)
    assert await memory_store.commit_event("u", "s", event) == "committed"

    with pytest.raises(EventConflictError, match="digest"):
        await memory_store.commit_event(
            "u", "s", memory_event(sequence=1, event_id="event", content="changed")
        )
    with pytest.raises(ValueError, match="sequence"):
        await memory_store.commit_event(
            "u", "s", event.model_copy(update={"sequence": 2})
        )


@pytest.mark.asyncio
async def test_key_components_are_validated_before_persistence(
    memory_store: AsyncRedisMemoryStore,
) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await memory_store.reserve_event("../u", "s", "event", "a" * 64)
    with pytest.raises(ValueError, match="event_id"):
        await memory_store.reserve_event("u", "s", "event/id", "a" * 64)
    with pytest.raises(ValueError, match="digest"):
        await memory_store.reserve_event("u", "s", "event", "z" * 64)


def test_memory_event_fixture_digest_is_well_formed() -> None:
    event: MemoryEvent = memory_event()
    assert event.sha256 == sha256(event.content.encode()).hexdigest()
