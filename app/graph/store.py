"""
Entity graph store, rewritten to the OKF schema (PLAN.md §4.5, §14, and the
schema discussion this migration implements). One JSON file per entity,
organized by type, under:

    wikis/{wiki_id}/{type}/{slug}.okf.json

e.g. user_id=u_123, type=person, title="Alice Chen":

    wikis/u_123/person/alice-chen.okf.json

wiki_id is the type-scoped identifier used everywhere (relations' `target`,
traverse() entry points, etc): "person/alice-chen".

    {
      "okf_version": "1.0",
      "wiki_id": "person/alice-chen",
      "type": "person",
      "title": "Alice Chen",
      "aliases": ["Alice"],
      "compact": "Alice Chen — engineer on Project Orion.",
      "summary": "Software engineer on the Orion team. Based in Taipei.",
      "facts": [
        {"fact_id": "fact_0001", "text": "Works on Project Orion.",
         "confidence": 1.0, "evidence": [], "created_at": "...", "updated_at": "..."}
      ],
      "relations": [
        {"relation_id": "rel_0001", "target": "project/orion",
         "category": "related_to", "label": "works_on", "weight": 1.0,
         "reason": "works on", "evidence": [], "fact_ids": ["fact_0001"],
         "created_at": "...", "updated_at": "..."}
      ],
      "status": "active",
      "merged_into": null,
      "metadata": {
        "significance": 0.5, "last_accessed": "...", "created_at": "...",
        "updated_at": "...", "user_id": "u_123"
      }
    }

Every write also updates the wiki manifest (app/graph/manifest.py — avoids
directory scans, doubles as a compact-view cache) and appends to the wiki
ops log (app/graph/ops_log.py — §8/ADR-005 audit trail). Both are best-effort
in the sense that if either write fails after the entity write already
succeeded, the entity file itself remains the source of truth (ADR-001) —
manifest/ops drift is a rebuild-the-derived-state problem, not data loss.

Concurrency: same read-modify-write + ETag retry pattern as before
(storage_backend.ConflictError). See that module's docstring for the
known limitation under multi-process concurrent writers to the SAME entity.
"""

from __future__ import annotations

from app.graph.keys import wiki_key, wiki_prefix

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.graph.components import build_adjacency, connected_components
from app.graph.embeddings import EmbeddingIndex
from app.graph.manifest import ManifestEntry, WikiManifest
from app.graph.ops_log import WikiOp, WikiOpsLog
from app.graph.conflicts import ConflictStore
from app.storage.backend import ConflictError, StorageBackend

logger = logging.getLogger("memory_backend.graph")

_attach_lock = threading.Lock()


def _attach(backend: StorageBackend, attr: str, factory):
    """Get-or-create a per-backend singleton (see EntityGraphStore.__init__).
    Write modes / flush tuning come from Settings so they stay configurable
    without threading them through every call site."""
    existing = getattr(backend, attr, None)
    if existing is not None:
        return existing
    with _attach_lock:
        existing = getattr(backend, attr, None)
        if existing is not None:
            return existing
        from app.config import get_settings
        st = get_settings()
        mode = (st.manifest_write_mode if attr in ("_wiki_manifest", "_embedding_index")
                else st.ops_log_write_mode)
        obj = factory(backend, mode=mode,
                      flush_interval=st.flush_interval_seconds,
                      max_pending=st.flush_max_pending)
        setattr(backend, attr, obj)
        return obj


def _lock_for_entity(backend: StorageBackend, user_id: str, wiki_id: str) -> threading.Lock:
    """Get-or-create the lock guarding ONE entity's read-modify-write cycle
    end to end (see _mutate below) -- not just its final write.

    Why this exists: MIRAGE_VERIFY_CONDITIONAL_WRITES's re-read-before-write
    only closes the race for the WRITE step; two writers can still both read
    the same stale state before either writes, and the second writer's
    result silently overwrites the first's (a lost update) even with
    verification on, in the narrow window between the read and the lock
    mirage_backend takes around its own write. This lock closes that window
    completely for same-process writers by covering the ENTIRE cycle, which
    is what actually lets MIRAGE_VERIFY_CONDITIONAL_WRITES=false be safe
    rather than merely faster: this deployment runs as one process, so
    same-process is the case that matters (chat and autocapture writing the
    same entity at the same moment), and this protects it at zero per-write
    network cost -- unlike the read it replaces.

    Registered on the BACKEND, not the EntityGraphStore instance: a fresh
    EntityGraphStore is built per request (see EntityGraphStore.__init__),
    so a per-instance lock dict would give every request its own, mutually
    invisible registry -- exactly the bug this is meant to avoid.
    """
    registry = getattr(backend, "_entity_locks", None)
    if registry is None:
        with _attach_lock:
            registry = getattr(backend, "_entity_locks", None)
            if registry is None:
                registry = {}
                backend._entity_locks = registry
    key = (user_id, wiki_id)
    lock = registry.get(key)
    if lock is None:
        with _attach_lock:
            lock = registry.get(key)
            if lock is None:
                lock = threading.Lock()
                registry[key] = lock
    return lock


MAX_WRITE_RETRIES = 5
# Cap on how many default seeds subgraph() backfills across components, so a
# wiki with many tiny/singleton components can't flood a first-page-load
# response and crowd out real detail from the largest cluster.
_DEFAULT_SEED_CAP = 10
# Tracks the Google Cloud Open Knowledge Format spec version these files
# target. Was "1.0", which is not a version OKF has ever had.
OKF_VERSION = "0.1"

# Body sections this serialiser generates. They are regenerated on every
# write, so _deserialize must strip them before recovering the author's
# summary — otherwise each round-trip would append them to the prose again.
_FACTS_HEADING = "Facts"
_RELATIONS_HEADING = "Relations"
_CITATIONS_HEADING = "Citations"
_STATUS_HEADING = "Status"

# Where the structured data lives.
#
#   "frontmatter" — one self-contained file. Fewest writes, but the YAML block
#                   runs to ~40 lines and reads as a database dump rather than
#                   the index card OKF §4.1 describes.
#   "companion"   — the .md carries ONLY the spec's own fields and points at a
#                   sibling .okf.json via `resource`. Two objects per entity,
#                   so one extra write per change, in exchange for a document
#                   that matches the spec's own examples.
#
# Both are conformant; this is a judgement about what the files are for.
OKF_MODE_FRONTMATTER = "frontmatter"
OKF_MODE_COMPANION = "companion"

class MalformedEntityError(ValueError):
    """An entity file on disk could not be parsed into an Entity.

    Subclasses ValueError so existing `except ValueError` handlers keep
    working, but is distinguishable for callers that want to report
    corruption as corruption rather than as a missing resource.
    """


VALID_TYPES = {
    "person", "organization", "project", "event",
    "concept", "artifact", "preference", "decision",
}

VALID_RELATION_CATEGORIES = {
    "related_to", "contradicts", "refines",
    "causes", "temporal_before", "temporal_after",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cjk_slug(name: str) -> str:
    """Deterministic CJK/latin fallback slug for a name with no ASCII chars.

    Collapsing a Chinese-only name to \"\" then \"entity\" made every such
    entity collide on e.g. person/entity. Encode the first codepoints + a hash
    so each script title gets its own stable, valid slug (mirrors
    registry.slugify_wiki).
    """
    cjk = [c for c in name.strip() if ord(c) > 127 and not c.isspace()]
    if not cjk:
        return "entity"
    code = "-".join(f"{ord(c):x}" for c in cjk[:4])
    import hashlib as _hl
    h = _hl.sha1(name.encode("utf-8")).hexdigest()[:8]
    raw = f"cjk-{code}-{h}"
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "entity"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or _cjk_slug(name)


def _title_key(title: str) -> str:
    """Canonical form for deciding whether two titles name the SAME entity.

    Case-folds and collapses whitespace, and strips punctuation only at the
    ENDS. Internal punctuation is preserved because it carries meaning:

        "Alice Chen" / "ALICE  chen!"  -> "alice chen"  same entity
        "C++"        / "C#"            -> "c++" / "c#"  DIFFERENT entities

    This is deliberately stricter than title_resolver._normalize, which strips
    all punctuation and therefore maps both "C++" and "C#" to "c". That is
    correct for fuzzy alias matching and wrong for deciding whether a write
    should overwrite an existing entity — using it here silently merged
    unrelated concepts.
    """
    text = re.sub(r"\s+", " ", title.strip().lower())
    return text.strip(".,;:!?'\"()[]{}<>-_/\\|`~@*")


def has_usable_slug(title: str) -> bool:
    """False when a title contains nothing a slug can be built from.

    _slugify falls back to the literal "entity" for such titles, so "!!!" and
    "???" both become `concept/entity` and collide with each other and with
    anything else unnameable. CJK/other-script titles (Chinese, Japanese,
    Korean) ARE usable -- they slug to a deterministic cjk-* id via
    _slugify, so they must not be rejected here.
    """
    s = title.strip().lower()
    if re.sub(r"[^a-z0-9]+", "", s):
        return True
    # CJK / non-Latin titles are nameable via _slugify's cjk fallback.
    return any(ord(c) > 127 and not c.isspace() for c in s)


class SlugConflictError(ValueError):
    """Two different titles produced the same wiki_id.

    Subclasses ValueError so existing handlers keep returning 4xx rather than
    500, but is distinguishable so the API can answer 409 with both titles.
    """

    def __init__(self, wiki_id: str, existing_title: str, incoming_title: str,
                 suggestion: str):
        self.wiki_id = wiki_id
        self.existing_title = existing_title
        self.incoming_title = incoming_title
        self.suggestion = suggestion
        super().__init__(
            f"{incoming_title!r} and the existing {existing_title!r} both map to "
            f"{wiki_id!r}. Refusing to overwrite. Either write to {wiki_id!r} "
            f"directly if they are the same thing, choose a more distinct title, "
            f"or pass on_conflict='disambiguate' to create {suggestion!r}.")


def _default_compact(summary: str, max_len: int = 140) -> str:
    """
    Stopgap for the `compact` field until real summarization exists (see
    decision #3 in the schema discussion): first sentence of `summary`,
    truncated. Callers should pass an explicit `compact` once something
    better is available — this is just so the field is never empty.
    """
    text = summary.strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", text, maxsplit=1)
    candidate = parts[0] if parts else text
    if len(candidate) > max_len:
        candidate = candidate[: max_len - 1].rstrip() + "\u2026"
    return candidate


def _next_seq_id(prefix: str, existing_ids: list[str]) -> str:
    max_n = 0
    for eid in existing_ids:
        m = re.match(rf"^{re.escape(prefix)}_(\d+)$", eid)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}_{max_n + 1:04d}"


@dataclass
class Fact:
    fact_id: str
    text: str
    confidence: float = 1.0
    evidence: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id, "text": self.text, "confidence": self.confidence,
            "evidence": self.evidence, "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Fact":
        return Fact(
            fact_id=d["fact_id"], text=d.get("text", ""),
            confidence=float(d.get("confidence", 1.0)),
            evidence=list(d.get("evidence", [])),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
        )


@dataclass
class Relation:
    relation_id: str
    target: str                 # target wiki_id, e.g. "project/orion"
    category: str                # fixed enum — VALID_RELATION_CATEGORIES
    label: str = ""               # free-form domain verb, e.g. "works_on"
    weight: float = 1.0
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "relation_id": self.relation_id, "target": self.target, "category": self.category,
            "label": self.label, "weight": self.weight, "reason": self.reason,
            "evidence": self.evidence, "fact_ids": self.fact_ids,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Relation":
        return Relation(
            relation_id=d["relation_id"], target=d["target"],
            category=d.get("category", "related_to"), label=d.get("label", ""),
            weight=float(d.get("weight", 1.0)), reason=d.get("reason", ""),
            evidence=list(d.get("evidence", [])), fact_ids=list(d.get("fact_ids", [])),
            created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""),
        )


@dataclass
class Metadata:
    significance: float = 0.5
    last_accessed: str = ""
    created_at: str = ""
    updated_at: str = ""
    user_id: str = ""

    def to_dict(self) -> dict:
        return {
            "significance": self.significance, "last_accessed": self.last_accessed,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "user_id": self.user_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "Metadata":
        return Metadata(
            significance=float(d.get("significance", 0.5)),
            last_accessed=d.get("last_accessed", ""), created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            user_id=d.get("user_id", ""),
        )


def decay_score(last_accessed: str, significance: float = 0.5,
                lam: float = 0.15, now: datetime | None = None) -> float:
    """exp(-lambda * days_since_access / significance). Standalone so both
    Entity.decay_score() (needs a full entity) and search ranking (only ever
    has a ManifestEntry, which carries last_accessed but not the full entity)
    can share one formula rather than duplicating it."""
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_accessed)
    except (TypeError, ValueError):
        # Hand-edited, legacy, or partially-written file. This is called
        # unconditionally by the API serializer, so raising here turns one
        # bad file into a permanent opaque 500 on that entity.
        return 0.0
    days = max(0.0, (now - last).total_seconds() / 86400.0)
    sig = max(significance, 1e-3)
    return math.exp(-lam * days / sig)


@dataclass
class Entity:
    wiki_id: str
    type: str
    title: str
    aliases: list[str]
    compact: str
    summary: str
    facts: list[Fact]
    relations: list[Relation]
    metadata: Metadata
    okf_version: str = OKF_VERSION
    status: str = "active"
    merged_into: str | None = None

    def decay_score(self, now: datetime | None = None, lam: float = 0.15) -> float:
        """exp(-lambda * days_since_access / significance) — unchanged from before."""
        return decay_score(self.metadata.last_accessed, self.metadata.significance,
                           lam=lam, now=now)


class EntityGraphStore:
    def __init__(self, backend: StorageBackend):
        self.backend = backend
        # The manifest and ops log hold write-behind buffers, so they must
        # outlive a single request. app/deps.py builds a NEW EntityGraphStore
        # per request (only the backend is an lru_cache singleton), so
        # constructing them here directly would reset every buffer on every
        # call and defeat the batching entirely. Memoize them onto the backend
        # instead — buffer lifetime then correctly tracks connection lifetime.
        from app.config import get_settings
        self.okf_mode = get_settings().okf_mode
        self.manifest = _attach(backend, "_wiki_manifest", WikiManifest)
        self.ops_log = _attach(backend, "_wiki_ops_log", WikiOpsLog)
        self.embedding_index = _attach(backend, "_embedding_index", EmbeddingIndex)
        # Conflict records are written once per conflict (not append-heavy),
        # so a per-request instance is fine — no write-behind buffer to keep.
        self.conflicts = ConflictStore(backend)
        self._conflict_enabled = self._load_conflict_flag()
        # Per-request author for the ops log. The wiki-scoped router sets this
        # from the bearer token before a write; user-scoped routes leave it
        # unset and _log_op falls back to the scope (== the token-bound user).
        self._actor: str | None = None

    @staticmethod
    def _load_conflict_flag() -> bool:
        """Feature flag: conflict checking off by default so nothing changes
        unless a deployment opts in. Also turns itself off if semantica isn't
        importable."""
        try:
            from app.config import get_settings
            settings = get_settings()
        except Exception:
            return False
        if not settings.conflict_check_enabled:
            return False
        try:
            from app.graph.semantica_wrap import semantica_available
            return semantica_available()
        except Exception:
            return False

    def set_actor(self, actor: str | None) -> None:
        """Name who is performing writes in this request's scope."""
        self._actor = actor

    def flush(self) -> None:
        """Force all buffered derived state to storage. Called on app
        shutdown and by tests that assert on raw storage contents."""
        self.manifest.flush()
        self.ops_log.flush()

    def _detect_and_record_conflicts(self, user_id: str, op: str,
                                     context: dict) -> None:
        """Run Semantica-backed conflict detection for a write and persist any
        findings as OKF conflict records. Best-effort: a detection failure must
        never break the write itself. Returns nothing; records are queryable via
        list_conflicts()."""
        if not self._conflict_enabled:
            return
        try:
            from app.graph.semantica_wrap import detect_for_write
        except Exception:
            return
        try:
            records = detect_for_write(user_id, op, context)
        except Exception:
            logger.warning("conflict detection raised; skipping for %s %s",
                           op, user_id, exc_info=True)
            return
        for rec in records:
            if not rec.conflict_id:
                from app.graph.conflicts import new_conflict_id
                rec.conflict_id = new_conflict_id()
            try:
                self.conflicts.save(user_id, rec)
                self._log_op(user_id, "update_fact", user_id,
                             reason=f"conflict detected: {rec.type}",
                             evidence=rec.sources)
            except Exception:
                logger.warning("could not persist conflict record", exc_info=True)

    def list_conflicts(self, user_id: str, type_: str | None = None,
                       status: str | None = None,
                       entity: str | None = None) -> list:
        """All conflict records in a wiki, newest first, optional filters."""
        return self.conflicts.list(user_id, type_=type_, status=status, entity=entity)

    def get_conflict(self, user_id: str, conflict_id: str):
        """One conflict record by id."""
        return self.conflicts.get(user_id, conflict_id)

    def resolve_conflict(self, user_id: str, conflict_id: str,
                         resolution: str = "resolved") -> None:
        """Mark a conflict resolved."""
        self.conflicts.mark_resolved(user_id, conflict_id, resolution)

    # ---------- id / key helpers ----------

    @staticmethod
    def compute_wiki_id(type_: str, title: str) -> str:
        if type_ not in VALID_TYPES:
            raise ValueError(f"Unknown type: {type_!r} (expected one of {VALID_TYPES})")
        return f"{type_}/{_slugify(title)}"

    def _delete_files(self, user_id: str, wiki_id: str) -> None:
        self.backend.delete(self._key(user_id, wiki_id))
        # Unconditional: deleting a companion that was never written is a
        # no-op, and leaving one behind would resurrect stale facts if the
        # slug were reused.
        try:
            self.backend.delete(self._companion_key(user_id, wiki_id))
        except Exception:
            pass

    def _companion_key(self, user_id: str, wiki_id: str) -> str:
        return wiki_key(user_id, f"{wiki_id}.okf.json")

    def _key(self, user_id: str, wiki_id: str) -> str:
        return wiki_key(user_id, f"{wiki_id}.okf.md")

    # ---------- serialization ----------
    #
    # OKF is stored as markdown with YAML front-matter, not raw JSON:
    #   - Front-matter holds everything cascade/reconcile/the title resolver
    #     need to parse and filter programmatically (facts, relations,
    #     metadata, status) — this MUST stay strictly structured, same as
    #     it was under plain JSON, or those features silently break.
    #   - The body holds `summary` as plain markdown text, for readability
    #     and for dropping straight into an LLM prompt without translation.
    #   - `compact` stays in front-matter (it's a machine-facing field used
    #     by the manifest cache, not something meant to be read as prose).

    @staticmethod
    def _serialize(entity: Entity, mode: str = OKF_MODE_FRONTMATTER) -> bytes:
        """The .md document. In companion mode the structured extensions are
        omitted and `resource` points at the sibling JSON instead."""
        import yaml
        # Field order follows OKF v0.1 §4.1: the required `type` first, then
        # the recommended fields in the priority the spec lists them, then
        # producer extensions. The spec permits extra keys and requires
        # consumers to tolerate them, so facts/relations/metadata stay in
        # frontmatter — but everything a generic OKF consumer needs is above
        # them and in the documented spelling.
        front_matter = {
            "type": entity.type,                        # REQUIRED by §9
            "title": entity.title,
            "description": entity.compact,              # §4.1 one-line summary
            # list(...) not the same object: yaml.safe_dump emits an anchor
            # and alias (&id001 / *id001) when two keys share one list, which
            # is valid YAML but not something every consumer resolves — and
            # OKF's whole promise is that any parser can read the file.
            "tags": list(entity.aliases),               # §4.1 cross-cutting labels
            "timestamp": entity.metadata.updated_at,    # §4.1 ISO 8601 last change
        }
        if mode == OKF_MODE_COMPANION:
            # §4.1: "a URI that uniquely identifies the underlying asset the
            # concept describes". With the structured data in a sibling file,
            # `resource` finally has something true to point at — which is
            # exactly the pattern the spec's own BigQuery example uses.
            front_matter["resource"] = f"/{entity.wiki_id}.okf.json"
            front_matter["okf_version"] = entity.okf_version
        else:
            front_matter.update({
            # --- producer extensions (§4.1 "Extensions") ---
            # `aliases` and `compact` were dropped: they said exactly what
            # `tags` and `description` now say, and duplicating them made the
            # frontmatter look more like a database dump than the index card
            # §4.1 describes. _deserialize still reads the old names, so files
            # written before this change load unchanged.
            "okf_version": entity.okf_version,
            "wiki_id": entity.wiki_id,
            "facts": [f.to_dict() for f in entity.facts],
            "relations": [r.to_dict() for r in entity.relations],
            "status": entity.status,
            "merged_into": entity.merged_into,
            "metadata": entity.metadata.to_dict(),
            })
        yaml_block = yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=True)

        parts = [f"---\n{yaml_block}---\n", f"# {entity.title}\n"]
        body = entity.summary.strip()
        if body:
            parts.append(f"{body}\n")

        if entity.facts:
            # A table rather than a bare list: §4.2 asks producers to favour
            # structural markdown, and confidence is meaningless to a reader
            # if it only exists in the frontmatter they were not meant to read.
            parts.append("## " + _FACTS_HEADING + "\n")
            parts.append("| Fact | Confidence | Source |")
            parts.append("| --- | --- | --- |")
            rows = []
            for f in entity.facts:
                cite = ", ".join(f.evidence) if f.evidence else "—"
                text = f.text.replace("|", "\\|")
                rows.append(f"| {text} | {f.confidence:g} | {cite} |")
            parts.append("\n".join(rows) + "\n")

        if entity.relations:
            # OKF §5: relationships are expressed as ordinary markdown links
            # in the BODY, and §5.3 says a consumer building a graph treats
            # links as edges. Keeping the graph only in frontmatter would make
            # it invisible to every generic OKF consumer, which is most of the
            # point of adopting the format. Bundle-relative form per §5.1,
            # which the spec recommends because it survives file moves.
            parts.append("## " + _RELATIONS_HEADING + "\n")
            lines = []
            for r in entity.relations:
                verb = (r.label or r.category).replace("_", " ")
                note = f" — {r.reason}" if r.reason else ""
                lines.append(f"- **{verb}** [{r.target}](/{r.target}.md){note}")
            parts.append("\n".join(lines) + "\n")

        if entity.status != "active" or entity.merged_into:
            parts.append("## Status\n")
            if entity.merged_into:
                parts.append(f"Merged into [{entity.merged_into}]"
                             f"(/{entity.merged_into}.md).\n")
            else:
                parts.append(f"{entity.status.capitalize()} as of "
                             f"{entity.metadata.updated_at[:10]}.\n")

        citations = []
        for f in entity.facts:
            for ev in f.evidence:
                if ev not in citations:
                    citations.append(ev)
        if citations:
            # §8: sources backing claims in the body, numbered, at the bottom.
            parts.append("## " + _CITATIONS_HEADING + "\n")
            parts.append("\n".join(f"[{i}] {c}" for i, c in enumerate(citations, 1)) + "\n")

        return "\n".join(parts).encode("utf-8")

    @staticmethod
    def _serialize_companion(entity: Entity) -> bytes:
        """The structured data that companion mode keeps out of the markdown.

        Everything needed to reconstruct the Entity exactly — fact ids,
        timestamps, relation weights. Recovering these by parsing the body
        table back would be lossy and brittle; a stray pipe character in a
        fact would corrupt a row.
        """
        payload = {
            "okf_version": entity.okf_version,
            "wiki_id": entity.wiki_id,
            "type": entity.type,
            "title": entity.title,
            "aliases": list(entity.aliases),
            "compact": entity.compact,
            "facts": [f.to_dict() for f in entity.facts],
            "relations": [r.to_dict() for r in entity.relations],
            "status": entity.status,
            "merged_into": entity.merged_into,
            "metadata": entity.metadata.to_dict(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _deserialize(raw: bytes, companion: bytes | None = None) -> Entity:
        import re
        import yaml
        text = raw.decode("utf-8")
        m = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", text, re.DOTALL)
        if not m:
            raise MalformedEntityError("Malformed OKF markdown: missing front-matter block")
        d = yaml.safe_load(m.group(1)) or {}
        if isinstance(d, dict) and companion is not None:
            # Companion wins: in this mode the markdown deliberately carries
            # only the spec's fields, so anything structured must come from
            # the JSON. `description`/`tags` stay readable from either.
            try:
                extra = json.loads(companion.decode("utf-8"))
                if isinstance(extra, dict):
                    d = {**d, **extra}
            except json.JSONDecodeError as e:
                raise MalformedEntityError(
                    f"Companion JSON is unreadable: {e}") from e
        if not isinstance(d, dict):
            raise MalformedEntityError(
                f"Malformed OKF front-matter: expected a mapping, got {type(d).__name__}"
            )
        body = m.group(2)
        # Drop the sections _serialize generates from structured data. They
        # are rebuilt on every write, so leaving them in the summary would
        # append a fresh copy on each round-trip until the file was mostly
        # duplicated headings.
        body = re.split(
            rf"^##\s+(?:{_FACTS_HEADING}|{_RELATIONS_HEADING}|{_CITATIONS_HEADING}"rf"|{_STATUS_HEADING})\s*$",
            body, maxsplit=1, flags=re.MULTILINE)[0]
        # strip the leading "# Title" heading — it's derived/redundant with
        # front-matter's `title`, kept only for human readability
        body = re.sub(r"^#\s*.+\n+", "", body, count=1)
        # Everything below indexes into parsed YAML. A file that is valid
        # YAML but structurally wrong (missing wiki_id, a fact entry without
        # its keys, metadata replaced by a scalar) raises KeyError /
        # AttributeError / TypeError from here — none of which are
        # ValueError, so they used to escape every route's `except
        # ValueError` and surface as an opaque 500 on reads AND on deletes.
        # Normalise them into one error type that carries the actual cause.
        try:
            return Entity(
                wiki_id=d["wiki_id"], type=d.get("type", "concept"),
                title=d.get("title", d.get("wiki_id", "")),
                # Accept both spellings: `aliases`/`compact` are this
                # producer's extensions, `tags`/`description` are the OKF
                # names. Reading both means a file written by another OKF
                # producer round-trips rather than losing its summary.
                aliases=list(d.get("aliases") or d.get("tags") or []),
                compact=d.get("compact") or d.get("description", ""),
                summary=body.strip(),
                facts=[Fact.from_dict(f) for f in d.get("facts", [])],
                relations=[Relation.from_dict(r) for r in d.get("relations", [])],
                metadata=Metadata.from_dict(d.get("metadata", {})),
                okf_version=d.get("okf_version", OKF_VERSION),
                status=d.get("status", "active"), merged_into=d.get("merged_into"),
            )
        except MalformedEntityError:
            raise
        except (KeyError, AttributeError, TypeError, IndexError) as e:
            wid = d.get("wiki_id", "<unknown>") if isinstance(d, dict) else "<unknown>"
            raise MalformedEntityError(
                f"Corrupt entity file for {wid!r}: {type(e).__name__}: {e}"
            ) from e

    # ---------- core read/write with optimistic concurrency ----------

    def _read_raw(self, user_id: str, wiki_id: str):
        return self.backend.get_bytes(self._key(user_id, wiki_id))

    def _read_entity(self, user_id: str, wiki_id: str, raw=None):
        """Deserialise, pulling in the companion JSON if one exists.

        Always attempts the companion regardless of the configured mode, so a
        bundle written in one mode reads correctly under the other and a
        switch needs no migration.
        """
        raw = raw if raw is not None else self._read_raw(user_id, wiki_id)
        if raw is None:
            return None
        companion = None
        if self.okf_mode == OKF_MODE_COMPANION:
            got = self.backend.get_bytes(self._companion_key(user_id, wiki_id))
            companion = got.data if got else None
        return self._deserialize(raw.data, companion)

    def _entity_cache(self) -> dict:
        """Per-backend short-term (in-memory) entity cache: (user_id, wiki_id)
        -> deserialized Entity. Attached to the backend so its lifetime tracks
        the connection (one shared cache per process backend singleton), the
        same pattern as the manifest/ops-log buffers. Invalidated on writes.

        (Not via _attach: that helper passes manifest/write-mode kwargs that a
        plain dict factory doesn't accept.)"""
        cache = getattr(self.backend, "_entity_cache", None)
        if cache is None:
            from threading import Lock
            _lock = getattr(self.backend, "_entity_cache_lock", None)
            if _lock is None:
                _lock = Lock()
                setattr(self.backend, "_entity_cache_lock", _lock)
            with _lock:
                cache = getattr(self.backend, "_entity_cache", None)
                if cache is None:
                    cache = {}
                    setattr(self.backend, "_entity_cache", cache)
        return cache

    @staticmethod
    def _invalidate_entity(entity_cache: dict, user_id: str, wiki_id: str) -> None:
        entity_cache.pop((user_id, wiki_id), None)

    def _mutate(self, user_id: str, wiki_id: str, mutator) -> Entity:
        """
        Read-modify-write with ETag-based optimistic concurrency and retry.
        `mutator(entity_or_none) -> Entity`. Creates a fresh Entity if none
        exists yet (mutator must handle None — only upsert_entity's mutator does).

        The WHOLE cycle runs under this entity's lock (_lock_for_entity), not
        just the write: two same-process writers who both read stale state
        before either writes can otherwise lose one's update even with
        MIRAGE_VERIFY_CONDITIONAL_WRITES on, since that check only guards the
        write step. This closes that window for free, which is what makes
        running with verification OFF safe here rather than merely faster.
        """
        key = self._key(user_id, wiki_id)
        lock = _lock_for_entity(self.backend, user_id, wiki_id)
        with lock:
            last_error = None
            for _ in range(MAX_WRITE_RETRIES):
                current = self._read_raw(user_id, wiki_id)
                entity = self._read_entity(user_id, wiki_id, current) if current else None
                new_entity = mutator(entity)
                etag = current.etag if current else ""
                try:
                    if self.okf_mode == OKF_MODE_COMPANION:
                        # Companion first: a markdown file whose `resource` points
                        # at a JSON that does not exist yet is worse than a JSON
                        # nothing points at. The markdown keeps the conditional
                        # write, so it remains the concurrency control point.
                        self.backend.put_bytes(
                            self._companion_key(user_id, wiki_id),
                            self._serialize_companion(new_entity))
                    self.backend.put_bytes(
                        key, self._serialize(new_entity, self.okf_mode), if_match=etag)
                    # Invalidate the STM entity cache so the next read refetches
                    # the newly written state.
                    self._entity_cache().pop((user_id, wiki_id), None)
                    return new_entity
                except ConflictError as e:
                    last_error = e
                    continue
            raise last_error or RuntimeError("Failed to write entity after retries")

    def _resolve_slug_conflict(self, user_id: str, type_: str, title: str,
                               wiki_id: str, on_conflict: str) -> tuple[str, str | None]:
        """Returns (wiki_id_to_write, alias_to_record).

        Uses the manifest rather than reading entity files, so the check costs
        nothing on the hot path — the index is normally already in memory.
        """
        entry = self.manifest.get_entry(user_id, wiki_id)
        if entry is None or entry.status != "active":
            return wiki_id, None

        if _title_key(entry.title) == _title_key(title):
            # Same entity, typed differently. Merge, but keep the variant as an
            # alias so the other spelling still resolves. Discarding it was the
            # silent part of the original bug.
            variant = title if title != entry.title else None
            if variant and variant.lower() in {a.lower() for a in entry.aliases}:
                variant = None
            return wiki_id, variant

        suggestion = self._next_free_slug(user_id, wiki_id)
        if on_conflict == "error":
            raise SlugConflictError(wiki_id, entry.title, title, suggestion)
        if on_conflict == "disambiguate":
            logger.info("slug conflict: %r vs %r -> using %s",
                        entry.title, title, suggestion)
            return suggestion, None
        logger.warning("slug conflict: %r merged into %r at %s, incoming title "
                       "discarded (on_conflict='merge')", title, entry.title, wiki_id)
        return wiki_id, None

    def _next_free_slug(self, user_id: str, wiki_id: str) -> str:
        """`concept/c` -> `concept/c-2`, `-3`, ... first one not in the index."""
        for n in range(2, 1000):
            candidate = f"{wiki_id}-{n}"
            if self.manifest.get_entry(user_id, candidate) is None:
                return candidate
        raise ValueError(f"Could not find a free wiki_id near {wiki_id!r}")

    def _sync_manifest(self, user_id: str, entity: Entity) -> None:
        self.manifest.upsert_entry(user_id, ManifestEntry(
            wiki_id=entity.wiki_id, type=entity.type, title=entity.title,
            aliases=entity.aliases, path=self._key(user_id, entity.wiki_id),
            compact=entity.compact, status=entity.status,
            updated_at=entity.metadata.updated_at,
            last_accessed=entity.metadata.last_accessed,
            # Mirror the entity's outbound edges into the index so traversal
            # never has to open entity files. The entity file remains the
            # source of truth; this is derived and rebuildable.
            edges=[{"t": r.target, "c": r.category} for r in entity.relations],
        ))
        # Semantic-search fallback (see app/graph/embeddings.py): off by
        # default, and a no-op cost when off (is_embedding_enabled() short-
        # circuits before anything touches the model). Recomputed on every
        # write off the entity's own compact field, same "derived, never
        # authoritative" posture as the manifest above. Imported here rather
        # than at module scope, like every other embeddings.py consumer
        # (app/graph/search.py, app/verify/assessor.py) -- so monkeypatching
        # app.graph.embeddings.embed/is_embedding_enabled in tests reaches
        # this call site too, instead of a stale name bound at import time.
        from app.graph.embeddings import embed, is_embedding_enabled
        if is_embedding_enabled() and entity.compact:
            vector = embed(entity.compact)
            if vector is not None:
                self.embedding_index.upsert(user_id, entity.wiki_id, vector)

    def _log_op(self, user_id: str, op: str, wiki_id: str, reason: str = "",
                evidence: list[str] | None = None, field_: str | None = None,
                actor: str | None = None) -> None:
        # actor defaults to the scope (user_id): on the user-scoped API a
        # token is bound to its path user, so scope == author. Shared wikis
        # call store.set_actor(caller) before a write; prefer that when set.
        self.ops_log.append(user_id, WikiOp(
            op_id=self.ops_log._next_op_id(), op=op, wiki_id=wiki_id,
            reason=reason, evidence=evidence or [], field_name=field_,
            actor=actor if actor is not None
                  else (self._actor or user_id),
        ))

    # ---------- public API ----------

    def get_entity(self, user_id: str, wiki_id: str, touch: bool = True,
                    include_deleted: bool = False) -> Entity | None:
        # Short-term memory: a full entity read (1-2 S3 GETs) cached in
        # memory so repeated reads — the chat/retrieval hot path — return
        # sub-ms instead of ~750ms. Invalidated on any write to the entity.
        cache = self._entity_cache()
        key = (user_id, wiki_id)
        cached = cache.get(key)
        if cached is not None:
            entity = cached
        else:
            raw = self._read_raw(user_id, wiki_id)
            if raw is None:
                # READ REPAIR. The entity file is the source of truth (ADR-001)
                # and the manifest is a buffered cache of it, so the two can
                # diverge: a hard delete removes the file synchronously but the
                # matching manifest removal is only staged, and an abrupt process
                # exit (SIGKILL, closing the terminal) drops that staged change.
                # The result is a phantom — GET 404s while GET /wiki still lists
                # the entity and _stats still counts it, indefinitely.
                #
                # Rather than leave that until someone runs _rebuild_manifest, a
                # read that finds no file evicts the stale entry. Costs nothing
                # extra (we already know the file is gone) and converts a silent
                # permanent inconsistency into a self-correcting one.
                if self.manifest.get_entry(user_id, wiki_id) is not None:
                    logger.info("read repair: dropping stale manifest entry %s/%s", user_id, wiki_id)
                    self.manifest.remove_entry(user_id, wiki_id)
                cache.pop(key, None)
                return None
            entity = self._read_entity(user_id, wiki_id, raw)
            cache[key] = entity
        if entity.status == "deleted" and not include_deleted:
            # Tombstoned — treat exactly like a missing file. This is what
            # makes delete_entity()'s status flip instantly authoritative
            # everywhere, even before the file is physically removed or the
            # cascade has finished cleaning up other entities' relations.
            return None
        if touch:
            # Previously this did a full read-modify-write of the entity file
            # on EVERY read — 1 extra GET + 1 PUT per GET, and PUTs are 12.5x
            # the price of GETs on S3. last_accessed now lives authoritatively
            # in the (buffered) manifest, so a read costs zero writes. The
            # entity file's copy is refreshed on the next real write.
            now = _now_iso()
            entity.metadata.last_accessed = now
            self.manifest.touch(user_id, wiki_id, now)
        return entity

    @staticmethod
    def _touch(entity: Entity | None) -> Entity:
        if entity is None:
            raise ValueError("Cannot touch a nonexistent entity")
        entity.metadata.last_accessed = _now_iso()
        return entity

    def upsert_entity(
        self,
        user_id: str,
        type_: str,
        title: str,
        aliases: list[str] | None = None,
        summary_append: str | None = None,
        compact: str | None = None,
        significance: float | None = None,
        on_conflict: str = "error",
    ) -> Entity:
        """
        Creates the entity if it doesn't exist, or updates it if it does.
        `type_` is fixed at creation — see the schema discussion's decision #4
        on why type reclassification isn't supported (wiki_id is derived from
        type, so changing type would mean changing every relation that points here).

        SLUG COLLISIONS
        wiki_id is derived from the title, and _slugify discards every
        non-alphanumeric character, so distinct titles can collide: "C++" and
        "C#" both become `concept/c`. Previously the second write simply
        overwrote the first entity's title and merged into it, losing one
        concept with no error.

        Titles that differ only in case, spacing or trailing punctuation
        ("Alice Chen" / "ALICE chen!") DO name the same entity, and are still
        merged — but the variant spelling is recorded as an alias instead of
        being thrown away, so both forms resolve later.

        `on_conflict` controls what happens when the titles genuinely differ:
          "error"        (default) raise SlugConflictError. The caller decides.
          "disambiguate" create `concept/c-2` instead, keeping both.
          "merge"        merge into the existing entity, DISCARDING the incoming
                         title. The old implicit behaviour, now opt-in.
        """
        if on_conflict not in ("error", "disambiguate", "merge"):
            raise ValueError(
                f"on_conflict must be 'error', 'disambiguate' or 'merge', got {on_conflict!r}")
        if not has_usable_slug(title):
            # _slugify would fall back to the literal "entity", so every
            # unnameable title collides with every other one.
            raise ValueError(
                f"Title {title!r} contains no letters or digits, so no wiki_id can "
                f"be derived from it.")

        wiki_id = self.compute_wiki_id(type_, title)
        wiki_id, alias_variant = self._resolve_slug_conflict(
            user_id, type_, title, wiki_id, on_conflict)
        if alias_variant:
            aliases = list(aliases or []) + [alias_variant]
        # `is_new` used to be a separate _read_raw() call purely to pick an
        # ops-log verb — a whole extra round-trip per upsert. _mutate already
        # reads the current state, so let the mutator report it instead.
        seen: dict = {}

        def mutator(entity: Entity | None) -> Entity:
            seen["is_new"] = entity is None
            now = _now_iso()
            if entity is None:
                entity = Entity(
                    wiki_id=wiki_id, type=type_, title=title, aliases=list(aliases or []),
                    compact="", summary="", facts=[], relations=[],
                    metadata=Metadata(
                        significance=significance if significance is not None else 0.5,
                        last_accessed=now, created_at=now, updated_at=now,
                        user_id=user_id,
                    ),
                )
            if summary_append:
                sep = "\n\n" if entity.summary else ""
                entity.summary = f"{entity.summary}{sep}{summary_append.strip()}"
            if compact is not None:
                entity.compact = compact
            elif not entity.compact and entity.summary:
                entity.compact = _default_compact(entity.summary)
            if aliases:
                entity.aliases = sorted(set(entity.aliases) | set(aliases))
            if significance is not None:
                entity.metadata.significance = significance
            entity.status = "active"  # revives a tombstone if this (type, title) was deleted
            entity.metadata.last_accessed = now
            entity.metadata.updated_at = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "create" if seen.get("is_new") else "update_fact", wiki_id,
                     reason="upsert_entity")
        # Cross-type conflict detection. A write of type/title is a *type
        # conflict* when another active entity holds the same title under a
        # DIFFERENT type (e.g. decision/sofia-reyes vs person/sofia-reyes).
        # The incoming slug may be brand new, so we cannot rely on is_new:
        # instead resolve the existing type for this title from the manifest
        # and hand it to the detector (which only emits for different types).
        existing_type = None
        for e in self.manifest.list_entries(user_id):
            if e.status == "active" and e.type != type_ and e.title == title:
                existing_type = e.type
                break
        if not seen.get("is_new") or existing_type:
            self._detect_and_record_conflicts(user_id, "upsert_entity", {
                "entity": wiki_id,
                "type_": type_,
                "existing_type": existing_type or result.type,
                "type": type_,
                "value": title,
                "sample_values": [result.title],
                "evidence": [],
            })
        return result

    def add_fact(
        self, user_id: str, wiki_id: str, text: str,
        confidence: float = 1.0, evidence: list[str] | None = None,
    ) -> Entity:
        seen: dict = {}

        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot add a fact to nonexistent entity {wiki_id!r}")
            seen["entity"] = entity
            now = _now_iso()
            fact_id = _next_seq_id("fact", [f.fact_id for f in entity.facts])
            entity.facts.append(Fact(
                fact_id=fact_id, text=text, confidence=confidence,
                evidence=list(evidence or []), created_at=now, updated_at=now,
            ))
            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_fact", wiki_id, reason="add_fact",
                     evidence=evidence or [], field_="facts")
        existing = seen.get("entity")
        if existing is not None:
            self._detect_and_record_conflicts(user_id, "add_fact", {
                "entity": wiki_id,
                "text": text,
                "confidence": confidence,
                "evidence": evidence or [],
                "existing_facts": [
                    {"text": f.text, "confidence": f.confidence,
                     "evidence": f.evidence} for f in existing.facts[:-1]
                ],
            })
        return result

    def add_facts(self, user_id: str, wiki_id: str, facts: list[dict]) -> Entity:
        """Same as calling add_fact() once per item in `facts`, but as ONE
        read-modify-write instead of one per fact.

        Extraction routinely proposes 5-10+ facts for a single entity in one
        pass. Each add_fact() call is a full _mutate() -- one GET plus, under
        OKF_MODE=companion (the default), two PUTs (the .md companion and the
        .json) -- so N facts on one entity cost 3N network round trips to the
        object store where 3 would do. Measured: ~17-20s to land 9 facts on
        one entity, sequentially, dominating capture latency far more than
        the LLM call that produced them.

        `facts` is a list of {"text", "confidence", "evidence"} dicts, same
        shape add_fact()'s arguments have individually. Conflict detection
        still runs once per fact, against the entity's facts as they stood
        BEFORE this batch (matching add_fact()'s semantics: a fact is judged
        against what existed, not against its batch-mates).
        """
        if not facts:
            raise ValueError("add_facts() called with an empty list")
        seen: dict = {}

        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot add facts to nonexistent entity {wiki_id!r}")
            seen["original_facts"] = list(entity.facts)
            now = _now_iso()
            for f in facts:
                fact_id = _next_seq_id("fact", [x.fact_id for x in entity.facts])
                entity.facts.append(Fact(
                    fact_id=fact_id, text=f["text"],
                    confidence=f.get("confidence", 1.0),
                    evidence=list(f.get("evidence") or []),
                    created_at=now, updated_at=now,
                ))
            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        for f in facts:
            self._log_op(user_id, "update_fact", wiki_id, reason="add_fact",
                         evidence=f.get("evidence") or [], field_="facts")
        original_facts = seen.get("original_facts", [])
        for f in facts:
            self._detect_and_record_conflicts(user_id, "add_fact", {
                "entity": wiki_id,
                "text": f["text"],
                "confidence": f.get("confidence", 1.0),
                "evidence": f.get("evidence") or [],
                "existing_facts": [
                    {"text": g.text, "confidence": g.confidence, "evidence": g.evidence}
                    for g in original_facts
                ],
            })
        return result

    def update_fact(
        self, user_id: str, wiki_id: str, fact_id: str,
        text: str | None = None, confidence: float | None = None,
        evidence: list[str] | None = None,
    ) -> Entity:
        """Same read-modify-write pattern as everywhere else — one file read,
        edit one item in the facts[] list, one conditional write."""
        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot update a fact on nonexistent entity {wiki_id!r}")
            now = _now_iso()
            for f in entity.facts:
                if f.fact_id == fact_id:
                    if text is not None:
                        f.text = text
                    if confidence is not None:
                        f.confidence = confidence
                    if evidence is not None:
                        f.evidence = sorted(set(f.evidence) | set(evidence))
                    f.updated_at = now
                    entity.metadata.updated_at = now
                    entity.metadata.last_accessed = now
                    return entity
            raise ValueError(f"Fact {fact_id!r} not found on entity {wiki_id!r}")

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_fact", wiki_id, reason="update_fact", field_="facts")
        return result

    def remove_fact(self, user_id: str, wiki_id: str, fact_id: str) -> Entity:
        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot remove a fact from nonexistent entity {wiki_id!r}")
            before = len(entity.facts)
            entity.facts = [f for f in entity.facts if f.fact_id != fact_id]
            if len(entity.facts) == before:
                raise ValueError(f"Fact {fact_id!r} not found on entity {wiki_id!r}")
            now = _now_iso()
            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_fact", wiki_id, reason="remove_fact", field_="facts")
        return result

    def link_entities(
        self,
        user_id: str,
        source_wiki_id: str,
        target_wiki_id: str,
        category: str = "related_to",
        label: str = "",
        weight: float = 1.0,
        reason: str = "",
        fact_ids: list[str] | None = None,
        evidence: list[str] | None = None,
        bidirectional: bool = False,
    ) -> Entity:
        if category not in VALID_RELATION_CATEGORIES:
            raise ValueError(
                f"Unknown relation category: {category!r} (expected one of {VALID_RELATION_CATEGORIES})"
            )

        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot link from nonexistent entity {source_wiki_id!r}")
            now = _now_iso()
            # de-dupe: same (target, category) updates in place instead of duplicating
            for r in entity.relations:
                if r.target == target_wiki_id and r.category == category:
                    r.weight = weight
                    r.label = label or r.label
                    r.reason = reason or r.reason
                    r.fact_ids = sorted(set(r.fact_ids) | set(fact_ids or []))
                    r.evidence = sorted(set(r.evidence) | set(evidence or []))
                    r.updated_at = now
                    entity.metadata.updated_at = now
                    entity.metadata.last_accessed = now
                    return entity
            relation_id = _next_seq_id("rel", [r.relation_id for r in entity.relations])
            entity.relations.append(Relation(
                relation_id=relation_id, target=target_wiki_id, category=category,
                label=label, weight=weight, reason=reason,
                evidence=list(evidence or []), fact_ids=list(fact_ids or []),
                created_at=now, updated_at=now,
            ))
            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        result = self._mutate(user_id, source_wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_relation", source_wiki_id,
                     reason=reason or "link_entities", evidence=evidence or [], field_="relations")
        self._detect_and_record_conflicts(user_id, "link_entities", {
            "source": source_wiki_id,
            "target": target_wiki_id,
            "category": category,
            "label": label,
            "confidence": 1.0,
            "evidence": evidence or [],
            "existing_edges": [
                {"target": r.target, "category": r.category, "label": r.label}
                for r in result.relations if not (r.target == target_wiki_id and r.category == category)
            ],
        })

        if bidirectional:
            self.link_entities(
                user_id, target_wiki_id, source_wiki_id, category=category, label=label,
                weight=weight, reason=reason, fact_ids=fact_ids, evidence=evidence,
                bidirectional=False,
            )
        return result

    def get_edges(self, user_id: str, wiki_id: str, categories: list[str] | None = None) -> list[Relation]:
        entity = self.get_entity(user_id, wiki_id, touch=False)
        if entity is None:
            return []
        if categories is None:
            return list(entity.relations)
        return [r for r in entity.relations if r.category in categories]

    def update_relation(
        self, user_id: str, wiki_id: str, relation_id: str,
        label: str | None = None, weight: float | None = None,
        reason: str | None = None, fact_ids: list[str] | None = None,
        evidence: list[str] | None = None,
    ) -> Entity:
        """Direct update by relation_id — as opposed to link_entities()'s
        dedupe-by-(target,category) update path, this lets you edit a
        relation without needing to know/recompute its target+category key."""
        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot update a relation on nonexistent entity {wiki_id!r}")
            now = _now_iso()
            for r in entity.relations:
                if r.relation_id == relation_id:
                    if label is not None:
                        r.label = label
                    if weight is not None:
                        r.weight = weight
                    if reason is not None:
                        r.reason = reason
                    if fact_ids is not None:
                        r.fact_ids = sorted(set(r.fact_ids) | set(fact_ids))
                    if evidence is not None:
                        r.evidence = sorted(set(r.evidence) | set(evidence))
                    r.updated_at = now
                    entity.metadata.updated_at = now
                    entity.metadata.last_accessed = now
                    return entity
            raise ValueError(f"Relation {relation_id!r} not found on entity {wiki_id!r}")

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_relation", wiki_id, reason="update_relation", field_="relations")
        return result

    def remove_relation(self, user_id: str, wiki_id: str, relation_id: str, bidirectional: bool = False) -> Entity:
        """
        bidirectional=True also removes the mirrored relation on the target
        side, if one exists (same target->source pointer, same category).
        Without this, removing one side of a pair created via
        link_entities(bidirectional=True) leaves the other side dangling —
        the two copies aren't linked by any shared id, so this is a
        best-effort match on (target, category), not a guaranteed pair.
        """
        removed_target: str | None = None
        removed_category: str | None = None

        def mutator(entity: Entity | None) -> Entity:
            nonlocal removed_target, removed_category
            if entity is None:
                raise ValueError(f"Cannot remove a relation from nonexistent entity {wiki_id!r}")
            before = len(entity.relations)
            for r in entity.relations:
                if r.relation_id == relation_id:
                    removed_target, removed_category = r.target, r.category
            entity.relations = [r for r in entity.relations if r.relation_id != relation_id]
            if len(entity.relations) == before:
                raise ValueError(f"Relation {relation_id!r} not found on entity {wiki_id!r}")
            now = _now_iso()
            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_relation", wiki_id, reason="remove_relation", field_="relations")

        if bidirectional and removed_target is not None:
            mirror_entity = self.get_entity(user_id, removed_target, touch=False)
            if mirror_entity is not None:
                mirror = next(
                    (r for r in mirror_entity.relations
                     if r.target == wiki_id and r.category == removed_category),
                    None,
                )
                if mirror is not None:
                    self.remove_relation(user_id, removed_target, mirror.relation_id, bidirectional=False)

        return result

    def list_entities(self, user_id: str, type_filter: str | None = None,
                       include_deleted: bool = False) -> list[str]:
        """Reads from the manifest — no directory scan (§9.3). Tombstoned
        entities (status=='deleted') are excluded by default, same as get_entity()."""
        entries = self.manifest.list_entries(user_id, type_filter=type_filter)
        if not include_deleted:
            entries = [e for e in entries if e.status != "deleted"]
        return [e.wiki_id for e in entries]

    def delete_entity(self, user_id: str, wiki_id: str, cascade: bool = True,
                       hard_delete: bool = True) -> bool:
        """
        Tombstones the entity first (status='deleted'), as a single atomic
        write — this is the fix for the previously-documented race: instead
        of "delete the file, then separately cascade-clean other entities"
        (a two-step process with a real gap in the middle), the FIRST thing
        that happens is one conditional write that makes this entity
        instantly and consistently invisible everywhere (get_entity(),
        traverse(), list_entities() all check status, not just file
        existence). Everything after that — the cascade, and the physical
        file removal — can happen at whatever pace, crash partway, or even
        be skipped (hard_delete=False), and it no longer matters for
        correctness: no reader can ever observe this entity as "existing"
        again once the tombstone write lands.

        cascade=False skips cleaning up other entities' dangling relations —
        useful for bulk deletes where you'd rather run cascade_orphaned_relations()
        or the /wiki/_reconcile sweep once at the end.

        hard_delete=False leaves the tombstoned file in place permanently
        (an audit trail — this is what the OKF schema's `status` field is
        for) instead of reclaiming storage. Defaults to True to preserve
        prior behavior (the file is actually removed).
        """
        def mutator(entity: Entity | None) -> Entity:
            if entity is None or entity.status == "deleted":
                raise ValueError(f"Entity {wiki_id!r} not found")
            entity.status = "deleted"
            entity.metadata.updated_at = _now_iso()
            return entity

        try:
            result = self._mutate(user_id, wiki_id, mutator)
        except ValueError:
            return False  # didn't exist, or was already tombstoned/hard-deleted

        # From this point on, the entity is already invisible to every
        # reader — everything below is best-effort cleanup, not correctness.
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "delete", wiki_id, reason="tombstone")

        if cascade:
            try:
                self.cascade_orphaned_relations(user_id, wiki_id)
            except Exception:
                # The tombstone write above already succeeded — this entity
                # is correctly invisible everywhere regardless of what
                # happens here. A cascade failure means some OTHER entity
                # might keep a dangling relation for a while longer, which
                # is a storage-tidiness problem (see traverse()/get_entity()
                # status checks), not a correctness one — the /wiki/_reconcile
                # sweep will still catch it. Log and move on rather than
                # turning a successful delete into a failed API call.
                logger.warning(
                    "cascade_orphaned_relations raised while deleting %r — "
                    "delete itself still succeeded (tombstone already committed); "
                    "any resulting dangling relations will be caught by the next "
                    "/wiki/_reconcile sweep",
                    wiki_id, exc_info=True,
                )

        if hard_delete:
            self._delete_files(user_id, wiki_id)
            self.manifest.remove_entry(user_id, wiki_id)

        return True

    def merge_entities(self, user_id: str, source_wiki_id: str, target_wiki_id: str,
                       hard_delete: bool = False) -> dict:
        """Fold `source_wiki_id` into `target_wiki_id`: the source's facts,
        aliases, summary and relations are copied onto the target, every
        OTHER entity's relations pointing at the source are redirected to
        the target (not dropped), and the source is tombstoned with
        `merged_into` set to the target.

        This is the missing half of upsert_entity()'s duplicate-avoidance:
        that prevents most duplicates at write time via fuzzy title
        matching (see title_resolver.py), but two entities that were never
        fuzzy-matched — a nickname with no string overlap with the real
        name, or a duplicate that predates that fix — can only be found
        after the fact. This is how you fix one once you've found it.

        Both entities must already exist and be active; call the API/caller
        decides which is kept. Returns
        {"merged": bool, "target": wiki_id, "facts_moved": int,
         "relations_moved": int, "relations_redirected": int, "failed": str|None}.
        """
        if source_wiki_id == target_wiki_id:
            return {"merged": False, "target": target_wiki_id, "facts_moved": 0,
                    "relations_moved": 0, "relations_redirected": 0,
                    "failed": "source and target are the same entity"}

        source = self.get_entity(user_id, source_wiki_id, touch=False)
        if source is None:
            return {"merged": False, "target": target_wiki_id, "facts_moved": 0,
                    "relations_moved": 0, "relations_redirected": 0,
                    "failed": "source entity not found"}
        target_check = self.get_entity(user_id, target_wiki_id, touch=False)
        if target_check is None:
            return {"merged": False, "target": target_wiki_id, "facts_moved": 0,
                    "relations_moved": 0, "relations_redirected": 0,
                    "failed": "target entity not found"}
        if target_check.type != source.type:
            # type is fixed at creation everywhere else in this file (see
            # upsert_entity's docstring) -- a person absorbed into a project
            # would leave person-shaped facts sitting on a project entity,
            # which is confusing at best. Merging is for two records of the
            # SAME kind of thing that turned out to be duplicates.
            return {"merged": False, "target": target_wiki_id, "facts_moved": 0,
                    "relations_moved": 0, "relations_redirected": 0,
                    "failed": f"cannot merge a {source.type!r} into a "
                              f"{target_check.type!r} — types must match"}

        counts: dict = {}

        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Target entity disappeared mid-merge: {target_wiki_id!r}")
            now = _now_iso()

            existing_texts = {f.text.strip() for f in entity.facts}
            moved = 0
            for f in source.facts:
                if f.text.strip() in existing_texts:
                    continue
                fact_id = _next_seq_id("fact", [x.fact_id for x in entity.facts])
                entity.facts.append(Fact(
                    fact_id=fact_id, text=f.text, confidence=f.confidence,
                    evidence=list(f.evidence), created_at=f.created_at or now, updated_at=now,
                ))
                existing_texts.add(f.text.strip())
                moved += 1
            counts["facts_moved"] = moved

            new_aliases = set(entity.aliases)
            if source.title != entity.title:
                new_aliases.add(source.title)
            new_aliases |= set(source.aliases)
            entity.aliases = sorted(new_aliases)

            if source.summary and source.summary.strip() not in (entity.summary or ""):
                sep = "\n\n" if entity.summary else ""
                entity.summary = f"{entity.summary}{sep}{source.summary.strip()}"
            if not entity.compact and entity.summary:
                entity.compact = _default_compact(entity.summary)

            entity.metadata.significance = max(entity.metadata.significance,
                                               source.metadata.significance)

            # The target may already hold its OWN relation pointing at the
            # source (e.g. the two duplicates had been linked to each other
            # before anyone noticed they were the same entity). Left in
            # place, that becomes a self-loop the moment the source's id is
            # gone -- drop it here, symmetric with the "would become a
            # self-loop" skip below for the opposite direction.
            entity.relations = [r for r in entity.relations if r.target != source_wiki_id]

            rel_moved = 0
            for r in source.relations:
                if r.target == target_wiki_id:
                    continue  # would become a self-loop
                match = next((x for x in entity.relations
                             if x.target == r.target and x.category == r.category), None)
                if match is not None:
                    match.fact_ids = sorted(set(match.fact_ids) | set(r.fact_ids))
                    match.evidence = sorted(set(match.evidence) | set(r.evidence))
                    match.updated_at = now
                else:
                    relation_id = _next_seq_id("rel", [x.relation_id for x in entity.relations])
                    entity.relations.append(Relation(
                        relation_id=relation_id, target=r.target, category=r.category,
                        label=r.label, weight=r.weight, reason=r.reason,
                        evidence=list(r.evidence), fact_ids=list(r.fact_ids),
                        created_at=r.created_at or now, updated_at=now,
                    ))
                    rel_moved += 1
            counts["relations_moved"] = rel_moved

            entity.metadata.updated_at = now
            entity.metadata.last_accessed = now
            return entity

        try:
            result = self._mutate(user_id, target_wiki_id, mutator)
        except ValueError as e:
            # TOCTOU: target existed at the check above but is gone by the
            # time _mutate actually reads it (e.g. deleted by a concurrent
            # request). Nothing was written -- fail cleanly instead of a 500.
            return {"merged": False, "target": target_wiki_id, "facts_moved": 0,
                    "relations_moved": 0, "relations_redirected": 0, "failed": str(e)}
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "update_fact", target_wiki_id,
                     reason=f"merge: absorbed {source_wiki_id!r}", field_="facts")

        def tombstone(entity: Entity | None) -> Entity:
            if entity is None or entity.status == "deleted":
                raise ValueError(f"Source entity {source_wiki_id!r} not found")
            entity.status = "deleted"
            entity.merged_into = target_wiki_id
            entity.metadata.updated_at = _now_iso()
            return entity

        try:
            tombstoned = self._mutate(user_id, source_wiki_id, tombstone)
        except ValueError as e:
            # The target-side merge above already landed and was flushed --
            # the source's content is safely on the target either way. Only
            # the tombstone step itself failed (source vanished between the
            # initial check and here). Report it rather than crash; the
            # source is already effectively gone, so there's nothing left to
            # retry other than re-running merge_entities if it turns out to
            # still exist.
            return {"merged": True, "target": target_wiki_id,
                    "facts_moved": counts.get("facts_moved", 0),
                    "relations_moved": counts.get("relations_moved", 0),
                    "relations_redirected": 0,
                    "failed": f"target updated, but tombstoning the source failed: {e}"}
        self._sync_manifest(user_id, tombstoned)
        self._log_op(user_id, "delete", source_wiki_id,
                     reason=f"merged into {target_wiki_id!r}")

        redirected = self._redirect_relations(user_id, source_wiki_id, target_wiki_id)
        counts["relations_redirected"] = len(redirected["fixed"])

        if hard_delete:
            self._delete_files(user_id, source_wiki_id)
            self.manifest.remove_entry(user_id, source_wiki_id)

        self.flush()
        return {"merged": True, "target": target_wiki_id,
                "facts_moved": counts.get("facts_moved", 0),
                "relations_moved": counts.get("relations_moved", 0),
                "relations_redirected": counts.get("relations_redirected", 0),
                "failed": None}

    def move_entity_between_wikis(self, src_wiki: str, tgt_wiki: str,
                                  entity_wiki_id: str) -> dict:
        """Move one entity (node) from src_wiki into tgt_wiki.

        Reads the entity from the source, writes a full copy (metadata, facts,
        relations) into the target under the SAME node id, then deletes it from
        the source. This is the primitive behind merge/split: an entity lives in
        exactly one wiki at a time, and moving it relocates its node + facts +
        relations. Relations to nodes that ALSO moved into tgt_wiki keep working
        (both targets now resolve inside tgt_wiki); relations to nodes that
        stayed in src_wiki are inherited by the copy so the link is preserved
        (the foreign node is referenced by id).

        Returns {"moved": bool, "target_node": str, "facts": int, "relations": int,
                 "failed": str | None}.
        """
        ent = self.get_entity(src_wiki, entity_wiki_id, touch=False)
        if ent is None or ent.status == "deleted":
            return {"moved": False, "target_node": entity_wiki_id,
                    "failed": "source entity not found"}
        try:
            created = self.upsert_entity(
                tgt_wiki, ent.type, ent.title,
                aliases=list(ent.aliases or []),
                summary_append=None,
                compact=ent.compact,
                significance=ent.metadata.significance
                if ent.metadata else None,
                on_conflict="merge")
        except Exception as e:
            return {"moved": False, "target_node": entity_wiki_id,
                    "failed": f"upsert: {e}"}

        n_facts = n_rels = 0
        for f in ent.facts:
            try:
                self.add_fact(tgt_wiki, created.wiki_id, f.text,
                              confidence=f.confidence)
                n_facts += 1
            except Exception:
                pass
        for rel in ent.relations:
            try:
                self.link_entities(tgt_wiki, created.wiki_id, rel.target,
                                   category=rel.category,
                                   label=rel.label or "", reason=rel.reason)
                n_rels += 1
            except Exception:
                pass
        try:
            self.delete_entity(src_wiki, entity_wiki_id, cascade=True,
                               hard_delete=True)
        except Exception as e:
            logger.warning("merge: delete source %r failed: %s",
                           entity_wiki_id, e)
        self.flush()
        return {"moved": True, "target_node": created.wiki_id,
                "facts": n_facts, "relations": n_rels, "failed": None}

    def cascade_orphaned_relations(self, user_id: str, deleted_wiki_id: str) -> dict:
        """
        Strips any relation targeting `deleted_wiki_id` from every other
        entity. O(n) in entity count per call — same cost profile as
        traverse()'s "no adjacency index yet" limitation; acceptable at the
        scale this is designed for. Only entities that actually had a
        dangling relation get rewritten (checked before calling _mutate,
        to avoid pointless writes/ETag churn on everything else).

        Resilient per-entity: if fixing one entity fails (e.g. a write
        conflict that exhausts its retries, or a transient storage error),
        that failure is logged and the scan continues — every OTHER
        affected entity still gets fixed in this same pass, rather than
        one bad entity aborting the whole cascade and leaving everything
        after it dangling too. Whatever still fails after this is caught
        by the next /wiki/_reconcile sweep regardless.

        Returns {"fixed": [...wiki_ids...], "failed": [...wiki_ids...]}.
        """
        fixed: list[str] = []
        failed: list[str] = []

        for other_wiki_id in self.list_entities(user_id):
            if other_wiki_id == deleted_wiki_id:
                continue
            try:
                current = self._read_raw(user_id, other_wiki_id)
                if current is None:
                    continue
                entity = self._read_entity(user_id, other_wiki_id, current)
                if not any(r.target == deleted_wiki_id for r in entity.relations):
                    continue  # no dangling reference here — skip the write entirely

                def mutator(e: Entity | None, _target=deleted_wiki_id) -> Entity:
                    if e is None:
                        raise ValueError(f"Entity disappeared mid-cascade: {other_wiki_id!r}")
                    e.relations = [r for r in e.relations if r.target != _target]
                    e.metadata.updated_at = _now_iso()
                    return e

                result = self._mutate(user_id, other_wiki_id, mutator)
                self._sync_manifest(user_id, result)
                self._log_op(
                    user_id, "update_relation", other_wiki_id,
                    reason=f"cascade: removed dangling relation to deleted entity {deleted_wiki_id!r}",
                    field_="relations",
                )
                fixed.append(other_wiki_id)
            except Exception:
                logger.warning(
                    "cascade_orphaned_relations: failed to clean %r's dangling "
                    "relation to deleted entity %r — continuing with the rest of "
                    "the scan; will be caught by the next /wiki/_reconcile sweep",
                    other_wiki_id, deleted_wiki_id, exc_info=True,
                )
                failed.append(other_wiki_id)

        return {"fixed": fixed, "failed": failed}

    def _redirect_relations(self, user_id: str, old_target: str, new_target: str) -> dict:
        """Like cascade_orphaned_relations(), but for merge_entities(): relations
        pointing at `old_target` are repointed at `new_target` instead of being
        dropped, so folding a duplicate entity into its canonical one doesn't
        quietly sever the rest of the graph. Same per-entity resilience as
        cascade_orphaned_relations() — one entity's write failure doesn't stop
        the rest of the scan.

        A collision (the entity already has a relation to `new_target` in the
        same category) merges fact_ids/evidence into the existing relation
        instead of creating a duplicate, mirroring link_entities()'s dedupe.

        Returns {"fixed": [...wiki_ids...], "failed": [...wiki_ids...]}.
        """
        fixed: list[str] = []
        failed: list[str] = []

        for other_wiki_id in self.list_entities(user_id):
            if other_wiki_id in (old_target, new_target):
                continue
            try:
                current = self._read_raw(user_id, other_wiki_id)
                if current is None:
                    continue
                entity = self._read_entity(user_id, other_wiki_id, current)
                if not any(r.target == old_target for r in entity.relations):
                    continue  # nothing to redirect here — skip the write entirely

                def mutator(e: Entity | None, _old=old_target, _new=new_target) -> Entity:
                    if e is None:
                        raise ValueError(f"Entity disappeared mid-redirect: {other_wiki_id!r}")
                    now = _now_iso()
                    kept: list[Relation] = []
                    by_key: dict[tuple[str, str], Relation] = {}
                    for r in e.relations:
                        if r.target != _old:
                            kept.append(r)
                            by_key[(r.target, r.category)] = r
                            continue
                        match = by_key.get((_new, r.category))
                        if match is not None:
                            match.fact_ids = sorted(set(match.fact_ids) | set(r.fact_ids))
                            match.evidence = sorted(set(match.evidence) | set(r.evidence))
                            match.updated_at = now
                        else:
                            r.target = _new
                            r.updated_at = now
                            kept.append(r)
                            by_key[(_new, r.category)] = r
                    e.relations = kept
                    e.metadata.updated_at = now
                    return e

                result = self._mutate(user_id, other_wiki_id, mutator)
                self._sync_manifest(user_id, result)
                self._log_op(
                    user_id, "update_relation", other_wiki_id,
                    reason=f"merge: redirected relation from {old_target!r} to {new_target!r}",
                    field_="relations",
                )
                fixed.append(other_wiki_id)
            except Exception:
                logger.warning(
                    "merge redirect: failed to repoint %r's relation from %r to %r — "
                    "continuing with the rest of the scan; will be caught by the "
                    "next /wiki/_reconcile sweep",
                    other_wiki_id, old_target, new_target, exc_info=True,
                )
                failed.append(other_wiki_id)

        return {"fixed": fixed, "failed": failed}

    def reconcile_dangling_relations(self, user_id: str) -> dict:
        """
        Full sweep, not targeted at one deleted entity: scans every entity's
        relations and strips any target that doesn't resolve to an existing
        entity. This is the safety net for whatever cascade_orphaned_relations()
        might have missed — a crash mid-cascade, a concurrent write landing in
        the gap between a delete and its cascade, manual data edits, etc.
        Idempotent — safe to run on a schedule or on demand; running it twice
        in a row with nothing new dangling is a cheap no-op (one manifest
        read + one read per entity, zero writes).

        Existence is checked against the manifest (one read, O(1) lookups
        per relation) rather than re-reading every target file, so this is
        O(entities) + O(relations), not O(entities * relations) in file reads.

        Resilient per-entity, same reasoning as cascade_orphaned_relations():
        one entity's write failure doesn't abort the rest of the sweep. Since
        this IS the last line of defense, letting one bad entity stop the
        whole sweep would be worse here than anywhere else — failures are
        logged and included in the returned dict instead.
        """
        existing_ids = set(self.list_entities(user_id))
        entities_scanned = 0
        entities_fixed = 0
        relations_removed = 0
        entities_failed: list[str] = []

        for wiki_id in existing_ids:
            entities_scanned += 1
            try:
                current = self._read_raw(user_id, wiki_id)
                if current is None:
                    continue  # manifest/storage briefly out of sync — next sweep will catch it
                entity = self._read_entity(user_id, wiki_id, current)
                dangling_targets = {r.target for r in entity.relations if r.target not in existing_ids}
                if not dangling_targets:
                    continue

                def mutator(e: Entity | None, _targets=dangling_targets) -> Entity:
                    if e is None:
                        raise ValueError(f"Entity disappeared mid-reconcile: {wiki_id!r}")
                    e.relations = [r for r in e.relations if r.target not in _targets]
                    e.metadata.updated_at = _now_iso()
                    return e

                removed_count = sum(1 for r in entity.relations if r.target in dangling_targets)
                result = self._mutate(user_id, wiki_id, mutator)
                self._sync_manifest(user_id, result)
                self._log_op(
                    user_id, "update_relation", wiki_id,
                    reason=f"reconcile: removed {removed_count} dangling relation(s) "
                           f"to {sorted(dangling_targets)}",
                    field_="relations",
                )
                entities_fixed += 1
                relations_removed += removed_count
            except Exception:
                logger.warning(
                    "reconcile_dangling_relations: failed to clean %r — "
                    "continuing with the rest of the sweep; rerun /wiki/_reconcile "
                    "to retry this entity",
                    wiki_id, exc_info=True,
                )
                entities_failed.append(wiki_id)

        return {
            "entities_scanned": entities_scanned,
            "entities_fixed": entities_fixed,
            "relations_removed": relations_removed,
            "entities_failed": entities_failed,
        }

    def traverse(
        self,
        user_id: str,
        entry_wiki_ids: list[str],
        max_depth: int = 1,
        max_nodes: int = 50,
        categories: list[str] | None = None,
    ) -> set[str]:
        """
        BFS over the manifest's adjacency index — not over entity files.

        Traversal returns wiki_ids only, never entity content, so the whole
        query can be answered from the index. Reading one object per node
        cost 31 storage operations for a depth-2 walk over 31 nodes; the
        index answers the same query from state that is normally already
        cached in memory.

        Entity files remain the source of truth. The index is derived and
        rebuildable via POST /wiki/_rebuild_manifest, so a stale index costs
        accuracy, never data. Entries written before the index existed carry
        edges=None and are read from disk once, in a single batch.

        Only wiki_ids that actually resolve to an existing entity are
        returned — a relation can point at something that's since been
        deleted (a dangling reference; delete_entity() cascades to clean
        these up, but nothing currently prevents transient staleness under
        concurrent writes), and a caller getting a wiki_id back from
        traverse() should be able to trust that get_entity() on it won't
        come back None.
        """
        entries = {e.wiki_id: e for e in self.manifest.list_entries(user_id)}

        # Nodes whose edges predate the adjacency index. Their manifest entry
        # has edges=None, meaning "unknown" rather than "none", so they still
        # need a file read. Batched, and normally empty.
        unindexed = [w for w, e in entries.items() if e.edges is None]
        legacy: dict[str, list[dict]] = {}
        if unindexed:
            raws = self.backend.get_many([self._key(user_id, w) for w in unindexed])
            for wid in unindexed:
                raw = raws.get(self._key(user_id, wid))
                if raw is None:
                    continue
                try:
                    ent = self._read_entity(user_id, wid, raw)
                except MalformedEntityError:
                    logger.warning("traverse: skipping unreadable entity %s/%s", user_id, wid)
                    continue
                if ent is None:
                    continue
                legacy[wid] = [{"t": r.target, "c": r.category} for r in ent.relations]

        def edges_of(wiki_id: str) -> list[dict]:
            entry = entries.get(wiki_id)
            if entry is None:
                return []
            if entry.edges is not None:
                return entry.edges
            return legacy.get(wiki_id, [])

        def exists(wiki_id: str) -> bool:
            entry = entries.get(wiki_id)
            return entry is not None and entry.status == "active"

        depth_of: dict[str, int] = {wid: 0 for wid in entry_wiki_ids}
        processed: set[str] = set()
        resolved: set[str] = set()
        frontier: list[str] = list(dict.fromkeys(entry_wiki_ids))

        while frontier and len(resolved) < max_nodes:
            batch = [w for w in frontier if w not in processed]
            if not batch:
                break
            processed.update(batch)

            next_frontier: list[str] = []
            for wiki_id in batch:
                if len(resolved) >= max_nodes:
                    break
                # A relation can point at something since deleted (a dangling
                # reference), and tombstoned entities must be invisible. The
                # index carries status, so both checks are answered here
                # rather than by opening the file.
                if not exists(wiki_id):
                    continue
                resolved.add(wiki_id)

                d = depth_of[wiki_id]
                if d >= max_depth:
                    continue

                for edge in edges_of(wiki_id):
                    target = edge.get("t")
                    if not target:
                        continue
                    if categories is not None and edge.get("c") not in categories:
                        continue
                    if target not in depth_of or depth_of[target] > d + 1:
                        depth_of[target] = d + 1
                    if target not in processed:
                        next_frontier.append(target)

            frontier = list(dict.fromkeys(next_frontier))

        return resolved

    # ---------- saved layout ----------
    #
    # Node positions, so the graph does not rearrange itself every time the
    # page loads. Kept in their own small object rather than in the manifest
    # or the entity files:
    #   - the manifest is derived and _rebuild_manifest legitimately discards
    #     it, which would silently throw positions away;
    #   - entity files are the source of truth and a drag is not a fact about
    #     the entity, so writing one per drag is both wrong and expensive.
    # This also means a stable layout is what you remember it being, which
    # matters more for finding things again than frame rate does.

    def _layout_key(self, user_id: str) -> str:
        return wiki_key(user_id, "_layout.json")

    def get_layout(self, user_id: str) -> dict:
        raw = self.backend.get_bytes(self._layout_key(user_id))
        if raw is None:
            return {}
        try:
            return json.loads(raw.data.decode("utf-8")).get("positions", {})
        except (ValueError, json.JSONDecodeError):
            logger.warning("layout for %s is unreadable; ignoring", user_id)
            return {}

    def save_layout(self, user_id: str, positions: dict) -> int:
        """Merge into whatever is stored, so saving a neighbourhood does not
        erase positions for nodes that were not on screen."""
        merged = self.get_layout(user_id)
        for wiki_id, pos in positions.items():
            try:
                merged[wiki_id] = {"x": round(float(pos["x"]), 1),
                                   "y": round(float(pos["y"]), 1)}
            except (KeyError, TypeError, ValueError):
                continue
        payload = json.dumps({"version": 1, "positions": merged},
                             ensure_ascii=False).encode("utf-8")
        self.backend.put_bytes(self._layout_key(user_id), payload)
        return len(merged)

    def subgraph(self, user_id: str, entry_wiki_ids: list[str] | None = None,
                 max_depth: int = 1, max_nodes: int = 60,
                 categories: list[str] | set[str] | None = None) -> dict:
        """A CLOSED neighbourhood: nodes plus the edges among them.

        This is what a graph view needs and what pagination cannot provide.
        Slicing a graph by index gives you a fragment whose edges point mostly
        at nodes you did not receive — unlayoutable, and indistinguishable from
        genuinely dangling references. A neighbourhood is closed: every returned
        edge has BOTH endpoints in the node set, so the client can render it
        without knowing anything else exists.

        Measured on a 500-node graph: 222 KB for the full listing, 3 KB for a
        depth-2 neighbourhood.

        UNDIRECTED, unlike traverse(). traverse answers "what does this lead
        to" and follows relations in their stated direction, which is right for
        reasoning. A view answers "what is connected to this", where an edge is
        visually symmetric — follow only outbound edges and half the graph
        becomes invisible depending on which end you happened to start from.

        Each node carries `degree` (total incident edges) and
        `hidden_neighbours` (how many of them were left out). Without the
        second, a UI cannot distinguish a leaf from a node whose neighbours
        simply were not fetched, so it cannot offer a meaningful expand
        affordance.

        With no `entry_wiki_ids`, seeds are the highest-degree nodes: hubs are
        the useful way into an unfamiliar graph, far more so than whichever
        entities happen to sort first.
        """
        wanted = set(categories) if categories is not None else None
        entries = {e.wiki_id: e for e in self.manifest.list_entries(user_id)
                   if e.status == "active"}

        # Undirected adjacency over active nodes only, so a tombstoned entity
        # breaks the path through it rather than being silently traversed.
        # build_adjacency() (app/graph/components.py) is the same computation
        # this used to do inline -- extracted so component detection below
        # reuses it instead of a second, possibly-inconsistent adjacency.
        adj = build_adjacency(list(entries.values()), categories=wanted)
        edge_cat: dict[tuple[str, str], str] = {}
        for wiki_id, entry in entries.items():
            for edge in entry.edges or []:
                target, cat = edge.get("t"), edge.get("c", "related_to")
                if target not in entries:
                    continue
                if wanted is not None and cat not in wanted:
                    continue
                edge_cat[(wiki_id, target)] = cat

        # Connected components over the WHOLE wiki (not just whatever this
        # call ends up returning) so every node's component id is stable and
        # meaningful regardless of which neighbourhood is currently in view.
        comps = connected_components(adj)
        comp_index = {w: i for i, comp in enumerate(comps) for w in comp}

        if not entry_wiki_ids:
            ranked = sorted(entries, key=lambda w: (-len(adj[w]), w))
            entry_wiki_ids = ranked[:3]
            # The top-3-by-degree default seeds are picked with no awareness
            # of components -- a small disconnected cluster can lose every
            # tiebreak and be completely absent from the response (not just
            # visually unseparated: never rendered at all, since this is the
            # exact path gui.html's first page load always takes). Backfill
            # one representative (highest local degree) from any component
            # not already covered, additively -- a single-component wiki's
            # `ranked[:3]` is therefore untouched, since the loop below finds
            # nothing left to add.
            covered = {comp_index[w] for w in entry_wiki_ids if w in comp_index}
            for i, comp in enumerate(comps):
                if len(entry_wiki_ids) >= _DEFAULT_SEED_CAP:
                    break
                if i in covered:
                    continue
                entry_wiki_ids.append(max(comp, key=lambda w: (len(adj[w]), w)))
                covered.add(i)
        entry_wiki_ids = [w for w in dict.fromkeys(entry_wiki_ids) if w in entries]
        if not entry_wiki_ids:
            return {"nodes": [], "edges": [], "seeds": [],
                    "truncated": False, "total_entities": len(entries)}

        # Level-synchronous BFS so max_nodes truncates at a depth boundary
        # rather than mid-level, which would give a lopsided picture.
        reached: list[str] = []
        seen: set[str] = set()
        frontier = list(entry_wiki_ids)
        depth = 0
        while frontier and len(reached) < max_nodes:
            nxt: list[str] = []
            for wiki_id in frontier:
                if wiki_id in seen or len(reached) >= max_nodes:
                    continue
                seen.add(wiki_id)
                reached.append(wiki_id)
                if depth < max_depth:
                    nxt.extend(n for n in sorted(adj[wiki_id]) if n not in seen)
            frontier = list(dict.fromkeys(nxt))
            depth += 1

        in_set = set(reached)
        nodes = []
        for wiki_id in sorted(in_set):
            node = entries[wiki_id].to_dict()
            neighbours = adj[wiki_id]
            node["degree"] = len(neighbours)
            node["hidden_neighbours"] = len(neighbours - in_set)
            node["component"] = comp_index.get(wiki_id)
            nodes.append(node)

        edges = [{"source": a, "target": b, "category": cat}
                 for (a, b), cat in sorted(edge_cat.items())
                 if a in in_set and b in in_set]

        return {
            "nodes": nodes,
            "edges": edges,
            "seeds": list(entry_wiki_ids),
            "truncated": bool(frontier) or len(reached) >= max_nodes,
            "total_entities": len(entries),
        }

    def stats(self, user_id: str) -> dict:
        """Entity and edge counts, served from the adjacency index.

        This used to read every entity file to count relations, so a graph of
        N entities cost N storage reads. The index already carries each
        entity's outbound edges, so the count is arithmetic over state that is
        normally in memory. Only entries predating the index (edges=None)
        still need a file read, and those are batched.
        """
        entries = [e for e in self.manifest.list_entries(user_id) if e.status == "active"]
        total_edges = 0
        unindexed = []
        for entry in entries:
            if entry.edges is None:
                unindexed.append(entry.wiki_id)
            else:
                total_edges += len(entry.edges)

        if unindexed:
            raws = self.backend.get_many([self._key(user_id, w) for w in unindexed])
            for wiki_id in unindexed:
                raw = raws.get(self._key(user_id, wiki_id))
                if raw is None:
                    continue
                try:
                    ent = self._read_entity(user_id, wiki_id, raw)
                    total_edges += len(ent.relations) if ent else 0
                except MalformedEntityError:
                    logger.warning("stats: skipping unreadable entity %s/%s", user_id, wiki_id)

        return {"entities": len(entries), "edges": total_edges}
