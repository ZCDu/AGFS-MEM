"""Compatibility imports for :mod:`dream.memory.items`."""

from dream.memory.items import (
    ENTRY_DELIMITER,
    MEMORY_ID_PATTERN,
    AtomicMemoryItem,
    InvalidReplaceTarget,
    entry_content,
    memory_id_for,
    parse_memory_items,
    require_atomic_text,
    resolve_replace_target,
)

__all__ = [
    "ENTRY_DELIMITER",
    "MEMORY_ID_PATTERN",
    "AtomicMemoryItem",
    "InvalidReplaceTarget",
    "entry_content",
    "memory_id_for",
    "parse_memory_items",
    "require_atomic_text",
    "resolve_replace_target",
]
