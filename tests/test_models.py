import pytest

from short_term_memory.models import (
    CompressionGeneration,
    MemoryContentType,
    MemoryEvent,
    MemorySummaryEnvelope,
    SessionCompressionContext,
    SessionCompressionMessage,
    SessionSummaryCoverage,
    SessionSummaryDocument,
)


def test_summary_envelope_round_trips_without_schema_change() -> None:
    document = SessionSummaryDocument(
        user_id="user-1",
        session_id="session-1",
        coverage=SessionSummaryCoverage(processed_message_count=8),
        current_goal=["finish Redis session support"],
        preferences=[],
        confirmed_facts=["journals preserve originals"],
        pending_items=[],
        attachment_references=[],
        compression_context=SessionCompressionContext(
            messages=[], tokens_before=100, tokens_after=60
        ),
        updated_at="2026-08-04T00:00:00+00:00",
    )

    restored = SessionSummaryDocument.model_validate_json(
        document.model_dump_json()
    )

    assert restored == document


def test_memory_event_rejects_unknown_fields_and_invalid_digest() -> None:
    event = MemoryEvent(
        sequence=1,
        event_id="event-1",
        role="user",
        content_type=MemoryContentType.CODE,
        content="print('ok')",
        metadata={"language": "python"},
        sha256="a" * 64,
        created_at="2026-08-06T00:00:00+00:00",
    )

    assert event.content_type is MemoryContentType.CODE
    with pytest.raises(ValueError):
        MemoryEvent.model_validate({**event.model_dump(), "unexpected": True})
    with pytest.raises(ValueError):
        MemoryEvent.model_validate({**event.model_dump(), "sha256": "bad"})


def test_compression_generation_requires_an_ordered_sequence_range() -> None:
    with pytest.raises(ValueError, match="through_sequence"):
        CompressionGeneration(
            generation=1,
            from_sequence=2,
            through_sequence=1,
            messages=[SessionCompressionMessage(role="system", content="opaque")],
            tokens_before=10,
            tokens_after=4,
            created_at="2026-08-06T00:00:00+00:00",
            ccr_expires_at="2026-08-06T12:00:00+00:00",
        )


def test_memory_summary_envelope_preserves_semantic_summary_and_generations() -> None:
    envelope = MemorySummaryEnvelope(
        version=1,
        compressed_through_sequence=1,
        compression_generations=[],
        current_goal=["ship the schema"],
        preferences=[],
        confirmed_facts=[],
        pending_items=[],
        attachment_references=[],
        updated_at="2026-08-06T00:00:00+00:00",
    )

    assert envelope.current_goal == ["ship the schema"]


def test_memory_event_metadata_is_an_immutable_defensive_copy() -> None:
    metadata = {"language": "python"}
    event = MemoryEvent(
        sequence=1,
        event_id="event-1",
        role="user",
        content_type="code",
        content="print('ok')",
        metadata=metadata,
        sha256="a" * 64,
        created_at="2026-08-06T00:00:00+00:00",
    )

    metadata["language"] = "rust"

    assert event.metadata == {"language": "python"}
    with pytest.raises(TypeError):
        event.metadata["language"] = "go"


def test_compression_generation_messages_are_immutable() -> None:
    generation = CompressionGeneration(
        generation=1,
        from_sequence=1,
        through_sequence=1,
        messages=[SessionCompressionMessage(role="system", content="opaque")],
        tokens_before=10,
        tokens_after=4,
        created_at="2026-08-06T00:00:00+00:00",
        ccr_expires_at="2026-08-06T12:00:00+00:00",
    )

    with pytest.raises(AttributeError):
        generation.messages.append(SessionCompressionMessage(role="user"))


def test_memory_summary_envelope_generations_are_immutable() -> None:
    envelope = MemorySummaryEnvelope(
        version=1,
        compressed_through_sequence=0,
        compression_generations=[],
        current_goal=[],
        preferences=[],
        confirmed_facts=[],
        pending_items=[],
        attachment_references=[],
        updated_at="2026-08-06T00:00:00+00:00",
    )

    with pytest.raises(AttributeError):
        envelope.compression_generations.append(object())
