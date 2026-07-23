"""Compatibility imports for the migrated Internship source client."""

from dream.integrations.internship.client import (
    InternshipMessage,
    InternshipRecord,
    InternshipSourceClient,
    SourceFetchError,
    parse_ndjson,
)

__all__ = [
    "InternshipMessage",
    "InternshipRecord",
    "InternshipSourceClient",
    "SourceFetchError",
    "parse_ndjson",
]
