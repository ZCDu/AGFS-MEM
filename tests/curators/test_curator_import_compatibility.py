"""Import contracts for the staged Curator-layer migration."""


def test_curator_types_and_prompts_keep_legacy_import_compatibility() -> None:
    from dream.curators.backend import (
        AICurationPlan,
        OpenAICuratorBackend,
        SemanticCuratorBackend,
        UserCurationPlan,
    )
    from dream.curators.llm_backend import (
        AICurationPlan as LegacyAICurationPlan,
        OpenAICuratorBackend as LegacyOpenAICuratorBackend,
        SemanticCuratorBackend as LegacySemanticCuratorBackend,
        UserCurationPlan as LegacyUserCurationPlan,
    )
    from dream.curators.prompts import AI_CURATOR_PROMPT, USER_CURATOR_PROMPT
    from dream.curators.writeback_prompts import (
        CHARACTER_WRITEBACK_PROMPT as LegacyCharacterWritebackPrompt,
        USER_PERSONA_WRITEBACK_PROMPT as LegacyUserPersonaWritebackPrompt,
    )
    from dream.hermes_compat.curator_prompts import (
        AI_CURATOR_PROMPT as LegacyAICuratorPrompt,
        USER_CURATOR_PROMPT as LegacyUserCuratorPrompt,
    )
    from dream.memory.writeback_prompts import (
        CHARACTER_WRITEBACK_PROMPT,
        USER_PERSONA_WRITEBACK_PROMPT,
    )

    assert LegacyAICurationPlan is AICurationPlan
    assert LegacyOpenAICuratorBackend is OpenAICuratorBackend
    assert LegacySemanticCuratorBackend is SemanticCuratorBackend
    assert LegacyUserCurationPlan is UserCurationPlan
    assert LegacyAICuratorPrompt is AI_CURATOR_PROMPT
    assert LegacyUserCuratorPrompt is USER_CURATOR_PROMPT
    assert LegacyCharacterWritebackPrompt is CHARACTER_WRITEBACK_PROMPT
    assert LegacyUserPersonaWritebackPrompt is USER_PERSONA_WRITEBACK_PROMPT
