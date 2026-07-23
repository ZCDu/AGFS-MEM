"""Compatibility imports for the migrated Internship source synchronization."""

from dream.integrations.internship.sync import (
    InternshipSourceSync,
    SourceClient,
    SourceSyncResult,
    SourceSyncState,
    SourceSyncStateStore,
    normalize_source_user_id,
    record_to_event,
)

__all__ = [
    "InternshipSourceSync",
    "SourceClient",
    "SourceSyncResult",
    "SourceSyncState",
    "SourceSyncStateStore",
    "normalize_source_user_id",
    "record_to_event",
]
