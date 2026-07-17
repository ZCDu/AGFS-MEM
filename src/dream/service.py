"""Application service connecting short-term conversation input to dreams."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dream.artifacts import AtomicArtifactStore
from dream.curators.ai import AICurator
from dream.curators.llm_backend import SemanticCuratorBackend
from dream.curators.registry import CuratorRegistry
from dream.curators.user import UserCurator
from dream.events import TaskCompletedEvent
from dream.ledger import EventLedger
from dream.managers.decision_cards import DecisionCardManager
from dream.managers.memory import MemoryManager
from dream.publication import PublicationStore
from dream.reports import DreamReportStore
from dream.review.backend import DeterministicReviewBackend, ReviewBackend
from dream.review.models import ArtifactKind
from dream.review.orchestrator import BackgroundReviewOrchestrator
from dream.review.progress import ReviewProgressStore
from dream.rollback import RollbackService
from dream.scheduler import DreamScheduler
from dream.scope import ScopeIds, resolve_scope
from dream.snapshots import SnapshotStore
from dream.sources.manual import manual_record_to_event, parse_manual_ndjson
from dream.validation.seeds import parse_seed_jsonl, seed_record_to_event


class DreamService:
    """Owns no Redis state; receives completed conversation batches by API."""

    def __init__(
        self,
        home: Path,
        backend: ReviewBackend | None = None,
        semantic_curator_backend: SemanticCuratorBackend | None = None,
        review_threshold: int = 10,
    ) -> None:
        self.home = home
        self.ledger = EventLedger(home / "ledger" / "events.jsonl")
        self.review_progress = ReviewProgressStore(
            home / "ledger" / "reviewed-events.jsonl"
        )
        self.scheduler = DreamScheduler(review_threshold=review_threshold)
        self.reviewer = BackgroundReviewOrchestrator(
            backend or DeterministicReviewBackend()
        )
        self.semantic_curator_backend = semantic_curator_backend
        self.recover_pending()

    def recover_pending(self) -> None:
        for event in self.ledger.read_all():
            if not self.review_progress.contains(event.event_id):
                self.scheduler.enqueue_unless_pending(event)

    def ingest_conversation(self, event: TaskCompletedEvent) -> None:
        paths = resolve_scope(self.home, event.scope)
        self.ledger.append(event)
        self.scheduler.enqueue(event)
        PublicationStore(paths).note_completed_event(event.event_id)

    def import_manual_ndjson(self, text: str) -> dict[str, int]:
        imported = 0
        duplicates = 0
        for record in parse_manual_ndjson(text):
            event = manual_record_to_event(record)
            if self.ledger.contains(event.event_id):
                duplicates += 1
                continue
            self.ingest_conversation(event)
            imported += 1
        return {"imported": imported, "duplicates": duplicates}

    def import_ai_seed_jsonl(self, text: str) -> dict[str, int]:
        imported = 0
        duplicates = 0
        for record in parse_seed_jsonl(text):
            event = seed_record_to_event(record)
            if self.ledger.contains(event.event_id):
                duplicates += 1
                continue
            self.ledger.append(event)
            self.scheduler.enqueue(event)
            imported += 1
        return {"imported": imported, "duplicates": duplicates}

    def start_context(self, ids: ScopeIds) -> dict[str, object]:
        paths = resolve_scope(self.home, ids)
        artifacts = AtomicArtifactStore(paths.agent_root)
        snapshot = SnapshotStore(paths, artifacts).create(ids)
        user_key = f"users/{ids.user_id}/USER.md"
        cards = [
            snapshot_file.content
            for key, snapshot_file in sorted(snapshot.files.items())
            if key.startswith("decision-cards/") and key.endswith(".md")
        ]
        return {
            "snapshot_id": snapshot.snapshot_id,
            "user_profile": snapshot.files[user_key].content,
            "decision_rules": snapshot.files["DECISION_RULES.md"].content,
            "decision_cards": cards,
        }

    def run_pending(self, ids: ScopeIds | None = None) -> list[dict[str, object]]:
        self.recover_pending()
        runs: list[dict[str, object]] = []
        while event := self.scheduler.pop_pending(ids):
            paths = resolve_scope(self.home, event.scope)
            snapshot = SnapshotStore(
                paths, AtomicArtifactStore(paths.agent_root)
            ).create(event.scope)
            ai_seed = any(ref.get("source") == "ai-seed" for ref in event.source_refs)
            allowed_tools = (
                frozenset({"decision_card_manage"})
                if ai_seed
                else frozenset({"memory_manage", "decision_card_manage"})
            )
            result = self.reviewer.review(
                event,
                allowed_tools=allowed_tools,
                snapshot=snapshot,
            )
            applied_kinds: list[str] = []
            rollback_ids: list[str] = []
            errors: list[str] = []
            for action in result.actions:
                try:
                    if action.kind is ArtifactKind.USER_PROFILE:
                        manager = MemoryManager(paths)
                    elif action.kind is ArtifactKind.DECISION_CARD:
                        manager = DecisionCardManager(paths)
                    else:
                        continue
                    manager.apply(action)
                    rollback_ids.append(manager.last_snapshot_id)
                    applied_kinds.append(action.kind.value)
                except Exception as exc:
                    errors.append(f"{action.kind.value}: {type(exc).__name__}: {exc}")
            if result.error:
                errors.append(result.error)
            run_id = f"review-{uuid4().hex}"
            if result.status == "failed":
                status = "failed"
            elif errors or result.status == "partial":
                status = "partial"
            else:
                status = "success"
            DreamReportStore(paths).write(
                {
                    "run_id": run_id,
                    "curator": "background_review",
                    "status": status,
                    "source_event_ids": [event.event_id],
                    "artifact_kinds": applied_kinds,
                    "rollback_snapshot_ids": rollback_ids,
                    "errors": errors,
                    "review_summary": result.summary,
                }
            )
            if status == "success":
                self.review_progress.append(event.event_id)
                self.scheduler.mark_review_accepted(event.scope)
            runs.append(
                {
                    "run_id": run_id,
                    "source_event_ids": [event.event_id],
                    "status": status,
                    "artifact_kinds": applied_kinds,
                    "errors": errors,
                }
            )
        return runs

    def run_curators(self, ids: ScopeIds) -> dict[str, object]:
        paths = resolve_scope(self.home, ids)
        ai_report = AICurator(
            paths, semantic_backend=self.semantic_curator_backend
        ).run()
        user_report = UserCurator(
            paths, semantic_backend=self.semantic_curator_backend
        ).run()
        return {"ai": ai_report, "user": user_report}

    def run_due_curators(self, now: datetime) -> dict[str, dict[str, object]]:
        active_scopes = {event.scope for event in self.ledger.read_all()}
        results: dict[str, dict[str, object]] = {}
        for ids in sorted(
            active_scopes,
            key=lambda item: (item.tenant_id, item.agent_id, item.user_id),
        ):
            paths = resolve_scope(self.home, ids)
            due = CuratorRegistry(
                [
                    AICurator(paths, semantic_backend=self.semantic_curator_backend),
                    UserCurator(paths, semantic_backend=self.semantic_curator_backend),
                ]
            ).run_due(now)
            if due:
                scope_key = f"{ids.tenant_id}/{ids.agent_id}/{ids.user_id}"
                results[scope_key] = due
        return results

    def rollback(self, ids: ScopeIds, snapshot_id: str) -> None:
        paths = resolve_scope(self.home, ids)
        RollbackService(paths).restore(snapshot_id)

    def read_report(self, ids: ScopeIds, run_id: str) -> str:
        paths = resolve_scope(self.home, ids)
        return AtomicArtifactStore(paths.agent_root).read_text(
            Path("dream-reports") / f"{run_id}.json"
        )
