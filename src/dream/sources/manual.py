"""Compatibility imports for the migrated manual conversation integration."""

from dream.integrations.manual import (
    ManualConversationRecord,
    ManualMessage,
    ManualSourceError,
    manual_record_to_event,
    parse_manual_ndjson,
)

__all__ = [
    "ManualConversationRecord",
    "ManualMessage",
    "ManualSourceError",
    "manual_record_to_event",
    "parse_manual_ndjson",
]
