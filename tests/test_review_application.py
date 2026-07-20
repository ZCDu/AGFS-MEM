import json
from pathlib import Path

from dream.artifacts import AtomicArtifactStore
from dream.curators.user import UserCurator
from dream.managers.decision_cards import DecisionCardManager
from dream.managers.memory import MemoryManager
from dream.reports import DreamReportStore
from dream.review.models import ArtifactKind, ReviewAction
from dream.rollback import RollbackService
from dream.scope import ScopeIds, resolve_scope


def test_user_profile_action_is_scoped_and_cites_source(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = MemoryManager(paths)
    version = manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={"action": "add", "content": "Prefers concise answers."},
            source_event_id="evt-1",
        )
    )
    content = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert version.sha256
    assert "Prefers concise answers." in content
    assert "evt-1" in content
    assert not (paths.agent_root / "users" / "bob" / "USER.md").exists()


def test_decision_card_is_a_human_readable_markdown_artifact(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = DecisionCardManager(paths)
    version = manager.apply(
        ReviewAction(
            kind=ArtifactKind.DECISION_CARD,
            tool_name="decision_card_manage",
            payload={
                "id": "verify-before-risky-action",
                "title": "高风险操作前先验证",
                "scenario": "用户要求执行难以回滚的操作",
                "signals": ["操作不可逆", "关键信息不足"],
                "principle": "先完成只读验证，再决定是否执行。",
                "outcome": "避免错误修改。",
                "boundaries": "低风险且可回滚的操作不需要反复确认。",
                "confidence": 0.82,
            },
            source_event_id="evt-2",
        )
    )
    card = paths.decision_cards_dir / "verify-before-risky-action.md"
    content = card.read_text(encoding="utf-8")
    assert version.sha256
    assert "# 高风险操作前先验证" in content
    assert "## 决策原则" in content
    assert "evt-2" in content


def test_memory_mutation_can_restore_the_previous_file(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(Path("users/alice/USER.md"), "Original profile.\n")
    manager = MemoryManager(paths)
    manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={"action": "add", "content": "Prefers concise answers."},
            source_event_id="evt-3",
        )
    )
    RollbackService(paths).restore(manager.last_snapshot_id)
    assert (paths.user_root / "USER.md").read_text(encoding="utf-8") == "Original profile.\n"


def test_memory_replace_accepts_curator_plural_evidence_comment(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = MemoryManager(paths)
    for event_id in ("evt-1", "evt-2"):
        manager.apply(
            ReviewAction(
                kind=ArtifactKind.USER_PROFILE,
                tool_name="memory_manage",
                payload={"action": "add", "content": "Prefers detailed steps."},
                source_event_id=event_id,
            )
        )
    UserCurator(paths).run()

    manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={
                "action": "replace",
                "old_content": "Prefers detailed steps.",
                "content": "Prefers short checklists.",
            },
            source_event_id="evt-9",
        )
    )

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert "Prefers short checklists." in profile
    assert "Prefers detailed steps." not in profile
    assert "evt-9" in profile


def test_report_store_writes_a_disk_verifiable_json_report(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    report_path = DreamReportStore(paths).write(
        {
            "run_id": "run-1",
            "status": "success",
            "source_event_ids": ["evt-1", "evt-2"],
        }
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_id"] == "run-1"
    assert report["source_event_ids"] == ["evt-1", "evt-2"]
