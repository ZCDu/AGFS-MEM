from short_term_memory.models import (
    SessionCompressionContext,
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
