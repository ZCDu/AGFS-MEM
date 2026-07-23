def test_legacy_integration_imports_reference_the_migrated_implementations() -> None:
    from dream.integrations.internship.client import (
        InternshipRecord,
        InternshipSourceClient,
        SourceFetchError,
        parse_ndjson,
    )
    from dream.integrations.internship.sync import (
        InternshipSourceSync,
        SourceSyncResult,
        SourceSyncState,
        SourceSyncStateStore,
        normalize_source_user_id,
        record_to_event,
    )
    from dream.integrations.manual import (
        ManualConversationRecord,
        ManualSourceError,
        manual_record_to_event,
        parse_manual_ndjson,
    )
    from dream.source_sync import (
        InternshipSourceSync as LegacyInternshipSourceSync,
    )
    from dream.source_sync import (
        SourceSyncResult as LegacySourceSyncResult,
    )
    from dream.source_sync import (
        SourceSyncState as LegacySourceSyncState,
    )
    from dream.source_sync import (
        SourceSyncStateStore as LegacySourceSyncStateStore,
    )
    from dream.source_sync import (
        normalize_source_user_id as legacy_normalize_source_user_id,
    )
    from dream.source_sync import record_to_event as legacy_record_to_event
    from dream.sources.internship import InternshipRecord as LegacyInternshipRecord
    from dream.sources.internship import (
        InternshipSourceClient as LegacyInternshipSourceClient,
    )
    from dream.sources.internship import SourceFetchError as LegacySourceFetchError
    from dream.sources.internship import parse_ndjson as legacy_parse_ndjson
    from dream.sources.manual import (
        ManualConversationRecord as LegacyManualConversationRecord,
    )
    from dream.sources.manual import ManualSourceError as LegacyManualSourceError
    from dream.sources.manual import (
        manual_record_to_event as legacy_manual_record_to_event,
    )
    from dream.sources.manual import (
        parse_manual_ndjson as legacy_parse_manual_ndjson,
    )

    assert LegacyManualConversationRecord is ManualConversationRecord
    assert LegacyManualSourceError is ManualSourceError
    assert legacy_manual_record_to_event is manual_record_to_event
    assert legacy_parse_manual_ndjson is parse_manual_ndjson
    assert LegacyInternshipRecord is InternshipRecord
    assert LegacyInternshipSourceClient is InternshipSourceClient
    assert LegacySourceFetchError is SourceFetchError
    assert legacy_parse_ndjson is parse_ndjson
    assert LegacyInternshipSourceSync is InternshipSourceSync
    assert LegacySourceSyncResult is SourceSyncResult
    assert LegacySourceSyncState is SourceSyncState
    assert LegacySourceSyncStateStore is SourceSyncStateStore
    assert legacy_normalize_source_user_id is normalize_source_user_id
    assert legacy_record_to_event is record_to_event
