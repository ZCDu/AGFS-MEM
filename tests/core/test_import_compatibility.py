"""Import contracts for the staged Core-layer migration."""


def test_core_types_are_available_from_new_and_legacy_paths() -> None:
    from dream.core.events import TaskCompletedEvent
    from dream.core.ledger import EventLedger
    from dream.core.scope import ScopeIds, ScopePaths, resolve_scope
    from dream.events import TaskCompletedEvent as LegacyTaskCompletedEvent
    from dream.ledger import EventLedger as LegacyEventLedger
    from dream.scope import (
        ScopeIds as LegacyScopeIds,
        ScopePaths as LegacyScopePaths,
        resolve_scope as legacy_resolve_scope,
    )

    assert LegacyTaskCompletedEvent is TaskCompletedEvent
    assert LegacyEventLedger is EventLedger
    assert LegacyScopeIds is ScopeIds
    assert LegacyScopePaths is ScopePaths
    assert legacy_resolve_scope is resolve_scope


def test_core_identifier_aliases_preserve_string_compatibility() -> None:
    from dream.core.identifiers import AgentId, EventId, TaskId, TenantId, UserId

    assert TenantId is str
    assert AgentId is str
    assert UserId is str
    assert EventId is str
    assert TaskId is str
