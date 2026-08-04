"""
Entity graph store, conforming to Google's OKF v0.2 specification.
One markdown file per entity, organized by type, under:

    {user_id}/wiki/{type}/{slug}.md

e.g. user_id=u_123, type=person, title="Alice Chen":

    default/u_123/wiki/person/alice-chen.md

wiki_id is the type-scoped identifier used everywhere (relations' `target`,
traverse() entry points, etc): "person/alice-chen".

Each entity file is a markdown document with YAML frontmatter:

```markdown
---
type: person
title: Alice Chen
description: Staff engineer on the retrieval team.
tags: [alice, engineer]
generated:
  by: memory_backend/1.0
  at: '2026-08-03T08:51:55+00:00'
status: stable
# --- producer extensions below ---
okf_version: '0.2'
wiki_id: person/alice-chen
facts:
- fact_id: fact_0001
  text: Works on Project Orion.
  confidence: 1.0
  evidence: []
  created_at: '...'
  updated_at: '...'
relations:
- relation_id: rel_0001
  target: project/orion
  category: related_to
  label: works_on
  weight: 1.0
  reason: works on
  evidence: []
  fact_ids: [fact_0001]
  created_at: '...'
  updated_at: '...'
merged_into: null
metadata:
  significance: 0.5
  last_accessed: '...'
  created_at: '...'
  updated_at: '...'
  user_id: u_123
---
# Alice Chen

Software engineer on the Orion team. Based in Taipei.

works on [project/orion](/project/orion.md) (works on).

- Works on Project Orion. (confidence: 1)
```

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

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.graph.manifest import ManifestEntry, WikiManifest
from app.graph.ops_log import WikiOp, WikiOpsLog
from app.storage.backend import ConflictError, StorageBackend

logger = logging.getLogger("memory_backend.graph")

_attach_lock = __import__("threading").Lock()


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
        mode = st.manifest_write_mode if attr == "_wiki_manifest" else st.ops_log_write_mode
        obj = factory(backend, mode=mode,
                      flush_interval=st.flush_interval_seconds,
                      max_pending=st.flush_max_pending)
        setattr(backend, attr, obj)
        return obj

MAX_WRITE_RETRIES = 5
# Tracks the Google Cloud Open Knowledge Format spec version these files
# target.
OKF_VERSION = "0.2"

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


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "entity"


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
    anything else unnameable.
    """
    return bool(re.sub(r"[^a-z0-9]+", "", title.strip().lower()))


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
    status: str = "stable"
    merged_into: str | None = None

    def decay_score(self, now: datetime | None = None, lam: float = 0.15) -> float:
        """exp(-lambda * days_since_access / significance) — unchanged from before."""
        now = now or datetime.now(timezone.utc)
        try:
            last = datetime.fromisoformat(self.metadata.last_accessed)
        except (TypeError, ValueError):
            # Hand-edited, legacy, or partially-written file. This is called
            # unconditionally by the API serializer, so raising here turns one
            # bad file into a permanent opaque 500 on that entity.
            return 0.0
        days = max(0.0, (now - last).total_seconds() / 86400.0)
        sig = max(self.metadata.significance, 1e-3)
        return math.exp(-lam * days / sig)


class EntityGraphStore:
    def __init__(self, backend: StorageBackend):
        self.backend = backend
        # The manifest and ops log hold write-behind buffers, so they must
        # outlive a single request. app/deps.py builds a NEW EntityGraphStore
        # per request (only the backend is an lru_cache singleton), so
        # constructing them here directly would reset every buffer on every
        # call and defeat the batching entirely. Memoize them onto the backend
        # instead — buffer lifetime then correctly tracks connection lifetime.
        self.manifest = _attach(backend, "_wiki_manifest", WikiManifest)
        self.ops_log = _attach(backend, "_wiki_ops_log", WikiOpsLog)

    def flush(self) -> None:
        """Force all buffered derived state to storage. Called on app
        shutdown and by tests that assert on raw storage contents."""
        self.manifest.flush()
        self.ops_log.flush()

    # ---------- id / key helpers ----------

    @staticmethod
    def compute_wiki_id(type_: str, title: str) -> str:
        if type_ not in VALID_TYPES:
            raise ValueError(f"Unknown type: {type_!r} (expected one of {VALID_TYPES})")
        return f"{type_}/{_slugify(title)}"

    def _delete_files(self, user_id: str, wiki_id: str) -> None:
        self.backend.delete(self._key(user_id, wiki_id))

    def _key(self, user_id: str, wiki_id: str) -> str:
        return f"{user_id}/wiki/{wiki_id}.md"

    # ---------- serialization ----------
    #
    # OKF v0.2 markdown with YAML frontmatter. Spec-defined fields come first
    # (§4.1, §5), then producer extensions (§4.1 "Extensions") for our internal
    # data model (facts, relations, metadata). Generic OKF consumers see the
    # spec fields and ignore the rest; our reconcile/traverse/cascade read
    # the extensions.

    @staticmethod
    def _serialize(entity: Entity) -> bytes:
        """Serialize an entity to an OKF v0.2 .md document.

        Frontmatter: spec fields (§4.1, §5) first, then producer extensions.
        Body: free-form markdown with inline relationship links (§6.1).
        """
        import yaml
        # --- OKF spec fields (§4.1, §5) ---
        fm: dict = {"type": entity.type}              # REQUIRED by §11
        if entity.title:
            fm["title"] = entity.title
        if entity.compact:
            fm["description"] = entity.compact        # §4.1 one-line summary
        if entity.aliases:
            fm["tags"] = list(entity.aliases)         # §4.1 cross-cutting labels
        # --- provenance & trust (§5.2) ---
        if entity.metadata.updated_at:
            fm["generated"] = {
                "by": "memory_backend/1.0",
                "at": entity.metadata.updated_at,
            }
        # --- lifecycle (§5.4) ---
        if entity.status != "stable":
            fm["status"] = entity.status
        # --- producer extensions (§4.1 "Extensions") ---
        fm.update({
            "okf_version": entity.okf_version,
            "wiki_id": entity.wiki_id,
            "facts": [f.to_dict() for f in entity.facts],
            "relations": [r.to_dict() for r in entity.relations],
            "merged_into": entity.merged_into,
            "metadata": entity.metadata.to_dict(),
        })
        yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)

        # --- body: free-form markdown ---
        parts = [f"---\n{yaml_block}---\n", f"# {entity.title}\n"]
        body = entity.summary.strip()
        if body:
            parts.append(f"{body}\n")

        # Inline relationship links (§6.1): bundle-relative form.
        if entity.relations:
            parts.append("\n")
            for r in entity.relations:
                verb = (r.label or r.category).replace("_", " ")
                note = f" ({r.reason})" if r.reason else ""
                parts.append(f"{verb} [{r.target}](/{r.target}.md){note}.  \n")

        if entity.merged_into:
            parts.append(f"\nMerged into [{entity.merged_into}](/{entity.merged_into}.md).\n")

        # Facts as bullet list (§4.2 favours structural markdown).
        if entity.facts:
            parts.append("\n")
            for f in entity.facts:
                text = f.text.replace("|", "\\|")
                parts.append(f"- {text} (confidence: {f.confidence:g})\n")

        return "\n".join(parts).encode("utf-8")

    @staticmethod
    def _deserialize(raw: bytes) -> Entity:
        """Deserialize an entity from its .md file.

        Frontmatter carries both spec fields and producer extensions.
        """
        import re
        import yaml
        text = raw.decode("utf-8")
        m = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", text, re.DOTALL)
        if not m:
            raise MalformedEntityError("Malformed OKF markdown: missing front-matter block")
        d = yaml.safe_load(m.group(1)) or {}
        if not isinstance(d, dict):
            raise MalformedEntityError(
                f"Malformed OKF front-matter: expected a mapping, got {type(d).__name__}"
            )
        body = m.group(2)
        # strip the leading "# Title" heading — it's derived/redundant with
        # front-matter's `title`, kept only for human readability
        body = re.sub(r"^#\s*.+\n+", "", body, count=1)

        # Resolve `generated.at` from the spec field, fall back to metadata.
        generated = d.get("generated", {})
        if isinstance(generated, dict):
            updated_at = generated.get("at", "")
        else:
            updated_at = ""
        meta = d.get("metadata", {})
        if isinstance(meta, dict) and not meta.get("updated_at") and updated_at:
            meta = {**meta, "updated_at": updated_at}

        try:
            return Entity(
                wiki_id=d.get("wiki_id", ""),
                type=d.get("type", "concept"),
                title=d.get("title", d.get("wiki_id", "")),
                # Accept both spellings for backward compat with old files.
                aliases=list(d.get("tags") or d.get("aliases") or []),
                compact=d.get("description") or d.get("compact", ""),
                summary=body.strip(),
                facts=[Fact.from_dict(f) for f in d.get("facts", [])],
                relations=[Relation.from_dict(r) for r in d.get("relations", [])],
                metadata=Metadata.from_dict(meta),
                okf_version=d.get("okf_version", OKF_VERSION),
                status=d.get("status", "stable"),
                merged_into=d.get("merged_into"),
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
        """Deserialise an entity from its .md file."""
        raw = raw if raw is not None else self._read_raw(user_id, wiki_id)
        if raw is None:
            return None
        return self._deserialize(raw.data)

    def _mutate(self, user_id: str, wiki_id: str, mutator) -> Entity:
        """
        Read-modify-write with ETag-based optimistic concurrency and retry.
        `mutator(entity_or_none) -> Entity`. Creates a fresh Entity if none
        exists yet (mutator must handle None — only upsert_entity's mutator does).
        """
        key = self._key(user_id, wiki_id)
        last_error = None
        for _ in range(MAX_WRITE_RETRIES):
            current = self._read_raw(user_id, wiki_id)
            entity = self._read_entity(user_id, wiki_id, current) if current else None
            new_entity = mutator(entity)
            etag = current.etag if current else ""
            try:
                self.backend.put_bytes(
                    key, self._serialize(new_entity), if_match=etag)
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
        if entry is None or entry.status != "stable":
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

    def _log_op(self, user_id: str, op: str, wiki_id: str, reason: str = "",
                evidence: list[str] | None = None, field_: str | None = None) -> None:
        self.ops_log.append(user_id, WikiOp(
            op_id=self.ops_log._next_op_id(), op=op, wiki_id=wiki_id,
            reason=reason, evidence=evidence or [], field_name=field_,
        ))

    # ---------- public API ----------

    def get_entity(self, user_id: str, wiki_id: str, touch: bool = True,
                    include_deleted: bool = False) -> Entity | None:
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
            return None
        entity = self._read_entity(user_id, wiki_id, raw)
        if entity.status == "deprecated" and not include_deleted:
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
            entity.status = "stable"  # revives a tombstone if this (type, title) was deprecated
            entity.metadata.last_accessed = now
            entity.metadata.updated_at = now
            return entity

        result = self._mutate(user_id, wiki_id, mutator)
        self._sync_manifest(user_id, result)
        self._log_op(user_id, "create" if seen.get("is_new") else "update_fact", wiki_id,
                     reason="upsert_entity")
        return result

    def add_fact(
        self, user_id: str, wiki_id: str, text: str,
        confidence: float = 1.0, evidence: list[str] | None = None,
    ) -> Entity:
        def mutator(entity: Entity | None) -> Entity:
            if entity is None:
                raise ValueError(f"Cannot add a fact to nonexistent entity {wiki_id!r}")
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
            entries = [e for e in entries if e.status != "deprecated"]
        return [e.wiki_id for e in entries]

    def delete_entity(self, user_id: str, wiki_id: str, cascade: bool = True,
                       hard_delete: bool = True) -> bool:
        """
        Marks the entity as deprecated (status='deprecated'), as a single atomic
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
        again once the deprecation write lands.

        cascade=False skips cleaning up other entities' dangling relations —
        useful for bulk deletes where you'd rather run cascade_orphaned_relations()
        or the /wiki/_reconcile sweep once at the end.

        hard_delete=False leaves the deprecated file in place permanently
        (an audit trail — this is what the OKF spec's `status` field is
        for) instead of reclaiming storage. Defaults to True to preserve
        prior behavior (the file is actually removed).
        """
        def mutator(entity: Entity | None) -> Entity:
            if entity is None or entity.status == "deprecated":
                raise ValueError(f"Entity {wiki_id!r} not found")
            entity.status = "deprecated"
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
            return entry is not None and entry.status == "stable"

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
        return f"{user_id}/wiki/_layout.json"

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
                   if e.status == "stable"}

        # Undirected adjacency over active nodes only, so a tombstoned entity
        # breaks the path through it rather than being silently traversed.
        adj: dict[str, set[str]] = {w: set() for w in entries}
        edge_cat: dict[tuple[str, str], str] = {}
        for wiki_id, entry in entries.items():
            for edge in entry.edges or []:
                target, cat = edge.get("t"), edge.get("c", "related_to")
                if target not in entries:
                    continue
                if wanted is not None and cat not in wanted:
                    continue
                adj[wiki_id].add(target)
                adj[target].add(wiki_id)
                edge_cat[(wiki_id, target)] = cat

        if not entry_wiki_ids:
            ranked = sorted(entries, key=lambda w: (-len(adj[w]), w))
            entry_wiki_ids = ranked[:3]
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
        entries = [e for e in self.manifest.list_entries(user_id) if e.status == "stable"]
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
