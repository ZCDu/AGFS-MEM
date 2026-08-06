from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from short_term_memory.compression.generations import (
    GenerationAssembler,
    GenerationPlanner,
)
from short_term_memory.models import CompressionGeneration
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from tests.factories import envelope, memory_event
from tests.storage.fake_redis import AsyncFakeRedis


@pytest.fixture
def repository() -> AsyncRedisMemoryStore:
    return AsyncRedisMemoryStore(AsyncFakeRedis())


@pytest.fixture
def journals(tmp_path) -> JournalStore:
    return JournalStore(VFSAdapter(tmp_path))


async def seed_originals(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
    sequences: range,
) -> None:
    for sequence in sequences:
        content = f"original-{sequence}"
        digest = sha256(content.encode("utf-8")).hexdigest()
        reservation = await repository.reserve_event("u", "s", f"e-{sequence}", digest)
        assert reservation.sequence == sequence
        event = memory_event(
            sequence=reservation.sequence,
            event_id=f"e-{sequence}",
            content=content,
        )
        journals.append_event("u", "s", event)
        await repository.commit_event("u", "s", event)


def envelope_from(candidate, marker: str):
    generation = CompressionGeneration(
        generation=candidate.expected_version + 1,
        from_sequence=candidate.from_sequence,
        through_sequence=candidate.through_sequence,
        messages=[{"role": "system", "content": marker}],
        tokens_before=100,
        tokens_after=25,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )
    return envelope(
        version=candidate.expected_version + 1,
        through=candidate.through_sequence,
        generations=[generation],
    )


@pytest.mark.asyncio
async def test_later_generations_contain_only_new_original_events(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    planner = GenerationPlanner(repository, journals, max_segments=8)
    await seed_originals(repository, journals, range(1, 101))
    first = await planner.plan_incremental("u", "s")
    assert first is not None
    assert await repository.compare_and_set_envelope(
        "u", "s", 0, envelope_from(first, marker="HR-1")
    )

    await seed_originals(repository, journals, range(101, 181))
    second = await planner.plan_incremental("u", "s")
    assert second is not None
    assert await repository.compare_and_set_envelope(
        "u", "s", 1, envelope_from(second, marker="HR-2")
    )

    await seed_originals(repository, journals, range(181, 241))
    third = await planner.plan_incremental("u", "s")
    assert third is not None

    assert [event.sequence for event in first.originals] == list(range(1, 101))
    assert [event.sequence for event in second.originals] == list(range(101, 181))
    assert [event.sequence for event in third.originals] == list(range(181, 241))
    rendered = json.dumps(
        [event.content for event in (*second.originals, *third.originals)]
    )
    assert "HR-1" not in rendered and "HR-2" not in rendered


@pytest.mark.asyncio
async def test_rebuild_reads_covered_originals_only_from_journal(
    repository: AsyncRedisMemoryStore,
    journals: JournalStore,
) -> None:
    await seed_originals(repository, journals, range(1, 4))
    stored = envelope(
        version=2,
        through=3,
        generations=[
            CompressionGeneration(
                generation=2,
                from_sequence=1,
                through_sequence=3,
                messages=[{"role": "assistant", "content": "HEADROOM_ONLY"}],
                tokens_before=30,
                tokens_after=10,
                created_at="2026-08-06T00:00:00+00:00",
                ccr_expires_at="2026-08-06T12:00:00+00:00",
            )
        ],
    )
    assert await repository.compare_and_set_envelope("u", "s", 0, stored)

    candidate = await GenerationPlanner(repository, journals, max_segments=8).plan_rebuild(
        "u", "s", 3
    )

    assert candidate is not None
    assert candidate.rebuild is True
    assert candidate.expected_version == 2
    assert [event.content for event in candidate.originals] == [
        "original-1",
        "original-2",
        "original-3",
    ]


def test_read_assembly_keeps_semantic_summary_unexpired_opaque_generations_and_recent_originals() -> None:
    fresh = CompressionGeneration(
        generation=2,
        from_sequence=3,
        through_sequence=4,
        messages=[{"role": "tool", "content": "FRESH", "tool_call_id": "opaque"}],
        tokens_before=20,
        tokens_after=8,
        created_at="2026-08-06T10:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )
    expired = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=2,
        messages=[{"role": "assistant", "content": "EXPIRED"}],
        tokens_before=20,
        tokens_after=8,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T01:00:00+00:00",
    )
    assembled = GenerationAssembler(max_segments=8).build_read_messages(
        envelope(
            version=2,
            through=4,
            generations=[expired, fresh],
        ).model_copy(update={"current_goal": ("finish",)}),
        (memory_event(sequence=4, event_id="e-4", content="recent overlap"),),
        datetime(2026, 8, 6, 11, tzinfo=timezone.utc),
    )

    assert assembled[0] == {
        "role": "system",
        "content": '{"current_goal":["finish"],"preferences":[],"confirmed_facts":[],"pending_items":[],"attachment_references":[]}',
    }
    assert assembled[1] == {
        "role": "tool",
        "content": "FRESH",
        "tool_call_id": "opaque",
    }
    assert assembled[2] == {"role": "user", "content": "recent overlap"}
    assert "EXPIRED" not in json.dumps(assembled)


def test_read_assembly_limits_opaque_generations_to_latest_segments() -> None:
    generations = tuple(
        CompressionGeneration(
            generation=index,
            from_sequence=index,
            through_sequence=index,
            messages=[{"role": "assistant", "content": f"segment-{index}"}],
            tokens_before=10,
            tokens_after=5,
            created_at="2026-08-06T00:00:00+00:00",
            ccr_expires_at="2026-08-07T00:00:00+00:00",
        )
        for index in range(1, 4)
    )

    assembled = GenerationAssembler(max_segments=2).build_read_messages(
        envelope(version=3, through=3, generations=generations),
        (),
        datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    assert [message["content"] for message in assembled[1:]] == [
        "segment-2",
        "segment-3",
    ]
