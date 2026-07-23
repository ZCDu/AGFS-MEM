"""Import contracts for the staged Governance-layer migration."""


def test_governance_types_are_available_from_new_and_legacy_paths() -> None:
    from dream.governance.canonicalizer import (
        InvalidKnowledgeProposal,
        KnowledgeAdapter,
    )
    from dream.governance.knowledge_adapter import (
        InvalidKnowledgeProposal as LegacyInvalidKnowledgeProposal,
        KnowledgeAdapter as LegacyKnowledgeAdapter,
    )
    from dream.governance.knowledge_router import (
        KnowledgeRouter as LegacyKnowledgeRouter,
    )
    from dream.governance.memory_policy import (
        AutoWritebackDecision as LegacyAutoWritebackDecision,
        GovernanceArtifact as LegacyGovernanceArtifact,
        GovernanceMode as LegacyGovernanceMode,
        MemoryGovernancePolicy as LegacyMemoryGovernancePolicy,
        RiskLevel as LegacyRiskLevel,
    )
    from dream.governance.policy import (
        AutoWritebackDecision,
        GovernanceArtifact,
        GovernanceMode,
        MemoryGovernancePolicy,
        RiskLevel,
    )
    from dream.governance.router import KnowledgeRouter

    assert LegacyInvalidKnowledgeProposal is InvalidKnowledgeProposal
    assert LegacyKnowledgeAdapter is KnowledgeAdapter
    assert LegacyKnowledgeRouter is KnowledgeRouter
    assert LegacyAutoWritebackDecision is AutoWritebackDecision
    assert LegacyGovernanceArtifact is GovernanceArtifact
    assert LegacyGovernanceMode is GovernanceMode
    assert LegacyMemoryGovernancePolicy is MemoryGovernancePolicy
    assert LegacyRiskLevel is RiskLevel
