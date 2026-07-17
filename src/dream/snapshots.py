"""Frozen context manifests that make dream updates next-task-only."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from dream.artifacts import AtomicArtifactStore
from dream.scope import ScopeIds, ScopePaths


@dataclass(frozen=True)
class SnapshotFile:
    content: str
    sha256: str


@dataclass(frozen=True)
class ContextSnapshot:
    snapshot_id: str
    scope: ScopeIds
    files: dict[str, SnapshotFile]
    created_at: str


class SnapshotStore:
    def __init__(self, paths: ScopePaths, artifacts: AtomicArtifactStore) -> None:
        self.paths = paths
        self.artifacts = artifacts

    def _relative_files(self, ids: ScopeIds) -> list[Path]:
        fixed = [
            Path("SOUL.md"),
            Path("DECISION_RULES.md"),
            Path("CHARACTER_DEFINITION.md"),
            Path("MEMORY.md"),
            Path("users") / ids.user_id / "USER.md",
            Path("users") / ids.user_id / "USER_PERSONA.md",
            Path("users") / ids.user_id / "MEMORY.md",
            Path("users") / ids.user_id / "TODOS.md",
        ]
        if self.paths.skills_dir.exists():
            fixed.extend(
                path.relative_to(self.paths.agent_root)
                for path in sorted(self.paths.skills_dir.rglob("*"))
                if path.is_file()
            )
        if self.paths.decision_cards_dir.exists():
            fixed.extend(
                path.relative_to(self.paths.agent_root)
                for path in sorted(self.paths.decision_cards_dir.glob("*.md"))
                if path.is_file()
            )
        return fixed

    def create(self, ids: ScopeIds) -> ContextSnapshot:
        files: dict[str, SnapshotFile] = {}
        for relative in self._relative_files(ids):
            key = relative.as_posix()
            content = self.artifacts.read_text(relative)
            files[key] = SnapshotFile(
                content=content,
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        canonical_hashes = json.dumps(
            {key: value.sha256 for key, value in sorted(files.items())},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        snapshot_id = hashlib.sha256(canonical_hashes.encode("utf-8")).hexdigest()
        snapshot = ContextSnapshot(
            snapshot_id=snapshot_id,
            scope=ids,
            files=files,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        payload = {
            "snapshot_id": snapshot.snapshot_id,
            "scope": {
                "tenant_id": ids.tenant_id,
                "agent_id": ids.agent_id,
                "user_id": ids.user_id,
            },
            "created_at": snapshot.created_at,
            "files": {
                key: {"sha256": value.sha256, "content": value.content}
                for key, value in sorted(files.items())
            },
        }
        self.artifacts.write_text(
            Path("snapshots") / snapshot_id / "context.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return snapshot

    def restore(self, snapshot_id: str, ids: ScopeIds) -> None:
        raw = self.artifacts.read_text(
            Path("snapshots") / snapshot_id / "context.json"
        )
        if not raw:
            raise FileNotFoundError(f"context snapshot not found: {snapshot_id}")
        payload = json.loads(raw)
        scope = payload.get("scope", {})
        expected = {
            "tenant_id": ids.tenant_id,
            "agent_id": ids.agent_id,
            "user_id": ids.user_id,
        }
        if scope != expected or payload.get("snapshot_id") != snapshot_id:
            raise ValueError("context snapshot identity mismatch")
        files = payload["files"]
        captured_cards = {
            key
            for key in files
            if key.startswith("decision-cards/") and key.endswith(".md")
        }
        if self.paths.decision_cards_dir.exists():
            for card in self.paths.decision_cards_dir.glob("*.md"):
                relative = card.relative_to(self.paths.agent_root).as_posix()
                if relative not in captured_cards:
                    card.unlink()
        for key, value in files.items():
            self.artifacts.write_text(Path(key), str(value["content"]))
