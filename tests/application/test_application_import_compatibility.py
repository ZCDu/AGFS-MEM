"""Import contracts for the staged Application-layer migration."""


def test_application_types_are_available_from_new_and_legacy_paths() -> None:
    from dream.application.closed_loop import (
        ClosedLoopCoordinator,
        ClosedLoopError,
        TaskStartBlocked,
    )
    from dream.application.deadline import DreamDeadline, DreamDeadlineExceeded
    from dream.application.progress import ReviewProgressStore
    from dream.application.scheduler import (
        DreamScheduler,
        PendingReviewBatch,
        ReviewSchedulePolicy,
        estimate_event_tokens,
    )
    from dream.application.service import DreamService
    from dream.closed_loop import (
        ClosedLoopCoordinator as LegacyClosedLoopCoordinator,
        ClosedLoopError as LegacyClosedLoopError,
        TaskStartBlocked as LegacyTaskStartBlocked,
    )
    from dream.deadline import (
        DreamDeadline as LegacyDreamDeadline,
        DreamDeadlineExceeded as LegacyDreamDeadlineExceeded,
    )
    from dream.review.progress import ReviewProgressStore as LegacyProgressStore
    from dream.scheduler import (
        DreamScheduler as LegacyDreamScheduler,
        PendingReviewBatch as LegacyPendingReviewBatch,
        ReviewSchedulePolicy as LegacyReviewSchedulePolicy,
        estimate_event_tokens as legacy_estimate_event_tokens,
    )
    from dream.service import DreamService as LegacyDreamService

    assert LegacyDreamService is DreamService
    assert LegacyClosedLoopCoordinator is ClosedLoopCoordinator
    assert LegacyClosedLoopError is ClosedLoopError
    assert LegacyTaskStartBlocked is TaskStartBlocked
    assert LegacyDreamDeadline is DreamDeadline
    assert LegacyDreamDeadlineExceeded is DreamDeadlineExceeded
    assert LegacyProgressStore is ReviewProgressStore
    assert LegacyDreamScheduler is DreamScheduler
    assert LegacyPendingReviewBatch is PendingReviewBatch
    assert LegacyReviewSchedulePolicy is ReviewSchedulePolicy
    assert legacy_estimate_event_tokens is estimate_event_tokens
