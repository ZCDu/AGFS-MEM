"""Import contracts for the staged Memory-layer migration."""


def test_memory_types_are_available_from_new_and_legacy_paths() -> None:
    from dream.artifacts import AtomicArtifactStore as LegacyAtomicArtifactStore
    from dream.managers.decision_cards import (
        DecisionCardManager as LegacyDecisionCardManager,
    )
    from dream.managers.memory import MemoryManager as LegacyMemoryManager
    from dream.managers.skills import SkillManager as LegacySkillManager
    from dream.memory.artifacts import AtomicArtifactStore
    from dream.memory.items import memory_id_for
    from dream.memory.managers.decision_cards import DecisionCardManager
    from dream.memory.managers.persona import MemoryManager
    from dream.memory.managers.skill_candidates import SkillManager
    from dream.memory.publication import PublicationStore
    from dream.memory.storage.reports import DreamReportStore
    from dream.memory.storage.rollback import RollbackService
    from dream.memory.storage.snapshots import SnapshotStore
    from dream.memory.writeback import WritebackService
    from dream.memory_items import memory_id_for as legacy_memory_id_for
    from dream.publication import PublicationStore as LegacyPublicationStore
    from dream.reports import DreamReportStore as LegacyDreamReportStore
    from dream.rollback import RollbackService as LegacyRollbackService
    from dream.snapshots import SnapshotStore as LegacySnapshotStore
    from dream.writeback import WritebackService as LegacyWritebackService

    assert LegacyAtomicArtifactStore is AtomicArtifactStore
    assert legacy_memory_id_for is memory_id_for
    assert LegacyMemoryManager is MemoryManager
    assert LegacyDecisionCardManager is DecisionCardManager
    assert LegacySkillManager is SkillManager
    assert LegacyPublicationStore is PublicationStore
    assert LegacyDreamReportStore is DreamReportStore
    assert LegacyRollbackService is RollbackService
    assert LegacySnapshotStore is SnapshotStore
    assert LegacyWritebackService is WritebackService
