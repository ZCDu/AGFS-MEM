"""Import contracts for the staged Extraction-layer migration."""


def test_extraction_types_are_available_from_new_and_legacy_paths() -> None:
    from dream.extraction.backend import DeterministicReviewBackend, ReviewBackend
    from dream.extraction.cache import ReviewStageCache
    from dream.extraction.classifier import TOOL_FOR_KIND
    from dream.extraction.llm_backend import OpenAIReviewBackend
    from dream.extraction.models import ArtifactKind, ReviewAction, ReviewResult
    from dream.extraction.prompts import DREAM_COMBINED_REVIEW_PROMPT
    from dream.extraction.provider_adapter import (
        CanonicalReview,
        InvalidReviewOutput,
        ReviewAdapter,
    )
    from dream.extraction.structured import (
        StructuredCompletionClient,
        StructuredCompletionError,
        StructuredProviderError,
        StructuredToolCall,
    )
    from dream.hermes_compat.prompts import (
        DREAM_COMBINED_REVIEW_PROMPT as LegacyPrompt,
    )
    from dream.review.adapter import (
        CanonicalReview as LegacyCanonicalReview,
        InvalidReviewOutput as LegacyInvalidReviewOutput,
        ReviewAdapter as LegacyReviewAdapter,
    )
    from dream.review.backend import (
        DeterministicReviewBackend as LegacyDeterministicReviewBackend,
        ReviewBackend as LegacyReviewBackend,
    )
    from dream.review.cache import ReviewStageCache as LegacyReviewStageCache
    from dream.review.classifier import TOOL_FOR_KIND as LEGACY_TOOL_FOR_KIND
    from dream.review.llm_backend import (
        OpenAIReviewBackend as LegacyOpenAIReviewBackend,
    )
    from dream.review.models import (
        ArtifactKind as LegacyArtifactKind,
        ReviewAction as LegacyReviewAction,
        ReviewResult as LegacyReviewResult,
    )
    from dream.structured_llm import (
        StructuredCompletionClient as LegacyStructuredCompletionClient,
        StructuredCompletionError as LegacyStructuredCompletionError,
        StructuredProviderError as LegacyStructuredProviderError,
        StructuredToolCall as LegacyStructuredToolCall,
    )

    assert LegacyDeterministicReviewBackend is DeterministicReviewBackend
    assert LegacyReviewBackend is ReviewBackend
    assert LegacyReviewStageCache is ReviewStageCache
    assert LEGACY_TOOL_FOR_KIND is TOOL_FOR_KIND
    assert LegacyOpenAIReviewBackend is OpenAIReviewBackend
    assert LegacyArtifactKind is ArtifactKind
    assert LegacyReviewAction is ReviewAction
    assert LegacyReviewResult is ReviewResult
    assert LegacyCanonicalReview is CanonicalReview
    assert LegacyInvalidReviewOutput is InvalidReviewOutput
    assert LegacyReviewAdapter is ReviewAdapter
    assert LegacyStructuredCompletionClient is StructuredCompletionClient
    assert LegacyStructuredCompletionError is StructuredCompletionError
    assert LegacyStructuredProviderError is StructuredProviderError
    assert LegacyStructuredToolCall is StructuredToolCall
    assert LegacyPrompt is DREAM_COMBINED_REVIEW_PROMPT
