"""Conflict records — the durable home for contradictions Semantica flags.

WHY A SEPARATE STORE (not a field on the entity)
    An entity file holds what is *true about the entity*. A conflict is a
    *disagreement between sources* about what is true: one claim says Alice's
    role is CTO, another says Engineer. Bundling disputes into the entity
    file would mix "what we believe" with "what is contested", and a
    resolution (one side wins) should not have to rewrite the entity's facts
    list to record its own history.

    So conflicts are first-class records, stored exactly like entities —
    one OKF document per conflict under:

        wikis/{wiki_id}/_conflicts/{conflict_id}.okf.md

    `_conflicts` (leading underscore) keeps them out of the type/ manifest
    namespace: they are not entities, and must not be listed as one.

PER-RECORD, NOT APPEND-ONLY
    The instructor requires OKF-formatted storage for the wiki graph. A
    conflict is immutable once written (a status field carries
    open -> resolved), so it does NOT need the segmented JSONL trick the ops
    log uses for append-heavy audit data. One document per conflict, written
    once, is the OKF-clean shape that avoids the quadratic-rewrite trap.

FORWARD-PORTABLE
    Every record carries `okf_version` and mirrors the frontmatter-vs-body
    split of app/graph/store.py._serialize: structured fields in the YAML
    block (so filters/read APIs parse without reading bodies), a markdown
    body for human readability.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.graph.keys import wiki_key, wiki_prefix
from app.storage.backend import StorageBackend

logger = logging.getLogger("memory_backend.conflicts")

# Kept as a constant shared with the detector so a consumer can map a
# Semantica ConflictType to a stable serialised string without importing the
# (heavier) semantica package just to read records.
CONFLICT_TYPES = [
    "type_conflict",        # same title, different entity type (person vs org)
    "value_conflict",       # same entity, structured field disagrees
    "relationship_conflict",# same pair/category, contradictory label
    "fact_conflict",        # free-text fact says the opposite of an existing fact
    "number_conflict",      # fact text changed a number (correction supersede)
]

_STATUS_OPEN = "open"
_STATUS_RESOLVED = "resolved"

# Body sections regenerated on every write, mirroring store.py's convention.
_CONFLICT_ID_HEADING = "Conflict"
_VALUES_HEADING = "Values"
_STATUS_HEADING = "Status"


@dataclass
class ConflictRecord:
    """A single detected contradiction, self-contained and traceable."""
    conflict_id: str
    type: str                          # one of CONFLICT_TYPES
    wiki_id: str                       # the scope (wiki) it belongs to
    entity: str                        # the entity wiki_id, e.g. person/alice-chen
    property_name: str = ""            # structured field, if any (role, label, ...)
    conflicting_values: list = field(default_factory=list)  # the disagreeing values
    sources: list[str] = field(default_factory=list)        # evidence refs
    confidence: float = 1.0
    severity: float = 0.5
    status: str = _STATUS_OPEN         # open | resolved
    resolution: str | None = None      # how it was resolved, when status=resolved
    recommended_action: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def new_conflict_id() -> str:
    """Short, sortable-by-age-ish id. Timestamp-prefixed so list_conflicts can
    sort chronologically without parsing the body."""
    return f"conf-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"


class ConflictStore:
    """Persist and query ConflictRecords as OKF documents.

    One file per conflict: wikis/{scope}/_conflicts/{conflict_id}.okf.md
    """

    def __init__(self, backend: StorageBackend):
        self.backend = backend

    # ---------- keys ----------

    def _conflicts_prefix(self, wiki_id: str) -> str:
        return wiki_key(wiki_id, "_conflicts/")

    def _key(self, wiki_id: str, conflict_id: str) -> str:
        return wiki_key(wiki_id, f"_conflicts/{conflict_id}.okf.md")

    @staticmethod
    def _safe_id(conflict_id: str) -> str:
        """Constrain ids so they never escape the conflicts folder or collide
        with path separators."""
        if not re.match(r"^conf-[A-Za-z0-9-]{6,64}$", conflict_id or ""):
            return "conf-" + re.sub(r"[^A-Za-z0-9-]", "", conflict_id)[:40]
        return conflict_id

    # ---------- write ----------

    def save(self, wiki_id: str, conflict: ConflictRecord,
             overwrite: bool = False) -> ConflictRecord:
        """Persist one conflict as an OKF document. Idempotent per id: save of
        an existing id is refused unless overwrite=True."""
        cid = self._safe_id(conflict.conflict_id)
        conflict.conflict_id = cid
        key = self._key(wiki_id, cid)
        if not overwrite and self.backend.get_bytes(key) is not None:
            raise ValueError(f"Conflict {cid!r} already exists; use overwrite=True.")
        self.backend.put_bytes(key, self._serialize(conflict))
        return conflict

    def mark_resolved(self, wiki_id: str, conflict_id: str,
                      resolution: str = "resolved") -> None:
        """Set a conflict's status to resolved by re-writing it."""
        record = self.get(wiki_id, conflict_id)
        if record is None:
            raise KeyError(f"No such conflict {conflict_id!r} in wiki {wiki_id!r}")
        record.status = _STATUS_RESOLVED
        record.resolution = resolution
        record.metadata["resolved_at"] = _now_iso()
        self.save(wiki_id, record, overwrite=True)

    # ---------- read ----------

    def get(self, wiki_id: str, conflict_id: str) -> ConflictRecord | None:
        raw = self.backend.get_bytes(self._key(wiki_id, self._safe_id(conflict_id)))
        return self._parse(raw) if raw is not None else None

    def list(self, wiki_id: str, type_: str | None = None,
             status: str | None = None, entity: str | None = None) -> list[ConflictRecord]:
        """All conflicts in a wiki, newest first. Optional filters."""
        out: list[ConflictRecord] = []
        prefix = self._conflicts_prefix(wiki_id)
        for key in self.backend.list_keys(prefix):
            if not key.endswith(".okf.md"):
                continue
            rec = self._parse(self.backend.get_bytes(key))
            if rec is None:
                continue
            if type_ is not None and rec.type != type_:
                continue
            if status is not None and rec.status != status:
                continue
            if entity is not None and rec.entity != entity:
                continue
            out.append(rec)
        out.sort(key=lambda r: (r.metadata or {}).get("created_at", ""), reverse=True)
        return out

    # ---------- serialization (OKF, mirroring store.py) ----------

    @staticmethod
    def _serialize(rec: ConflictRecord) -> bytes:
        import yaml
        front_matter = {
            "okf_version": "0.1",
            "type": "conflict",                    # OKF §9 required type
            "title": f"{rec.type} on {rec.entity}",
            "description": ConflictStore._body(rec),
            "tags": [rec.type],
            "timestamp": (rec.metadata or {}).get("created_at") or _now_iso(),
            # producer extensions (§4.1)
            "conflict_id": rec.conflict_id,
            "conflict_type": rec.type,
            "entity": rec.entity,
            "property_name": rec.property_name,
            "conflicting_values": rec.conflicting_values,
            "sources": rec.sources,
            "confidence": rec.confidence,
            "severity": rec.severity,
            "status": rec.status,
            "resolution": rec.resolution,
            "recommended_action": rec.recommended_action,
            "metadata": rec.metadata,
        }
        yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=True)
        parts = [f"---\n{yaml_block}---\n", f"# {rec.conflict_id}\n\n"]
        parts.append(ConflictStore._body(rec))
        parts.append("\n")
        return ("".join(parts)).encode("utf-8")

    @staticmethod
    def _body(rec: ConflictRecord) -> str:
        lines = [f"**{rec.type}** on `{rec.entity}`"]
        if rec.property_name:
            lines.append(f"Property: `{rec.property_name}`")
        if rec.conflicting_values:
            vals = "; ".join(str(v) for v in rec.conflicting_values[:10])
            lines.append(f"Values: {vals}")
        if rec.sources:
            lines.append("Sources: " + ", ".join(rec.sources[:10]))
        if rec.status == _STATUS_RESOLVED:
            lines.append(f"Status: resolved — {rec.resolution or 'resolved'}")
        return "\n\n".join(lines)

    @staticmethod
    def _parse(raw) -> ConflictRecord | None:
        if raw is None:
            return None
        try:
            text = raw.data.decode("utf-8")
        except Exception:
            return None
        if not text.lstrip().startswith("---"):
            return None
        import yaml
        try:
            m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            if not m:
                return None
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            logger.warning("could not parse conflict frontmatter", exc_info=True)
            return None
        cid = fm.get("conflict_id") or ""
        if not cid:
            return None
        return ConflictRecord(
            conflict_id=cid,
            type=fm.get("conflict_type") or "value_conflict",
            wiki_id="",
            entity=fm.get("entity") or "",
            property_name=fm.get("property_name") or "",
            conflicting_values=fm.get("conflicting_values") or [],
            sources=fm.get("sources") or [],
            confidence=float(fm.get("confidence", 1.0)),
            severity=float(fm.get("severity", 0.5)),
            status=fm.get("status") or _STATUS_OPEN,
            resolution=fm.get("resolution"),
            recommended_action=fm.get("recommended_action") or "",
            metadata=fm.get("metadata") or {},
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
