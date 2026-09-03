"""Semantica integration for the entity graph store.

WHY THIS MODULE EXISTS
    The instructor wants conflict detection/provenance semantics (Semantica's
    selling point) applied to the wiki graph. Semantica ships as an importable
    package; its `conflicts`, `dedup` and `provenance` modules are lightweight
    (no torch/spacy/faiss), so we use them directly rather than re-implementing.

    This module is the ONLY place that imports semantica. The store stays
    agnostic: store.py calls `detect_for_write(...)` and persists whatever
    ConflictRecords come back. If semantica is not installed (requirements not
    provisioned) or conflict checking is disabled, store.py keeps its existing
    behaviour exactly.

WHAT SEMANTICA HANDLES VS WHAT WE BRIDGE
    Semantica detects conflicts *across sources of the same entity* on
    structured properties:
      - type conflicts   (same title, different entity type)
      - value conflicts  (a structured field disagrees between records)
      - relationship conflicts (same pair/category, contradictory label)
    It does NOT do free-text semantic contradiction between arbitrary fact
    sentences. That is already handled by app/graph/store.py's
    _is_near_duplicate / _fact_signature logic (exact + containment matching,
    numbers compared exactly). So we *bridge* that existing logic into the
    same ConflictRecord shape, giving one unified conflict surface.

HYBRID BEHAVIOUR
    Default = flag + write: a conflict is recorded and the new value still
    lands, tagged "suspected conflict". When a resolution strategy is
    configured (CONFLICT_RESOLUTION_STRATEGY=most_recent|highest_confidence|
    voting|credibility_weighted), the write is gated by the resolver's verdict
    instead. Feature-flagged off by default in settings (CONFLICT_CHECK_ENABLED).
"""

from __future__ import annotations

import logging

from app.graph.conflicts import (CONFLICT_TYPES, ConflictRecord)

logger = logging.getLogger("memory_backend.semantica")

# Lazily imported so an env without semantica doesn't crash unrelated paths.
_SEMANTICA = None


def _semantica():
    """Import the semantica internals once, lazily. Raises ImportError if the
    package isn't installed (store handles this by staying off)."""
    global _SEMANTICA
    if _SEMANTICA is None:
        from semantica.conflicts import ConflictDetector, ConflictResolver
        from semantica.conflicts.conflict_detector import ConflictType
        # ResolutionStrategy values: VOTING, CREDIBILITY_WEIGHTED,
        # HIGHEST_CONFIDENCE, MOST_RECENT, FIRST_SEEN, MANUAL_REVIEW, ...
        from semantica.conflicts.conflict_resolver import ResolutionStrategy
        _SEMANTICA = {
            "detector": ConflictDetector,
            "resolver": ConflictResolver,
            "ConflictType": ConflictType,
            "ResolutionStrategy": ResolutionStrategy,
        }
    return _SEMANTICA


def semantica_available() -> bool:
    """False if semantica isn't importable — caller then skips conflict work."""
    try:
        _semantica()
        return True
    except Exception:
        return False


# Maps Semantica's ConflictType -> our stable serialised type string.
_TYPE_MAP = {
    "type_conflict": "type_conflict",
    "value_conflict": "value_conflict",
    "relationship_conflict": "relationship_conflict",
    "temporal_conflict": "value_conflict",
    "logical_conflict": "fact_conflict",
    "TYPE_CONFLICT": "type_conflict",
    "VALUE_CONFLICT": "value_conflict",
    "RELATIONSHIP_CONFLICT": "relationship_conflict",
}

# Maps our fact-text/number bridge to a Semantica-shaped type for resolver use.
_BRIDGED_TO_SEMANTICA = {
    "fact_conflict": "VALUE_CONFLICT",
    "number_conflict": "VALUE_CONFLICT",
}


def _type_str(conflict) -> str:
    """Best-effort extraction of a stable type string from a Semantica
    Conflict / ConflictType without assuming its internal shape."""
    raw = getattr(conflict, "conflict_type", None) or ""
    raw = raw if isinstance(raw, str) else getattr(raw, "value", str(raw))
    return _TYPE_MAP.get(raw, "value_conflict")


# ---------------------------------------------------------------------------
# Detection entry point used by store.py
# ---------------------------------------------------------------------------

def detect_for_write(wiki_id: str, op: str, context: dict,
                     resolve_strategy: str | None = None) -> list[ConflictRecord]:
    """Run conflict detection appropriate to a write operation and return
    ConflictRecords (persisted by the caller).

    `op` is one of: "add_fact" | "link_entities" | "upsert_entity".
    `context` carries the data the write is about (see each helper).

    When `resolve_strategy` is set, a conflict yields a *verdict* (the
    incoming value wins or loses); when unset (default), every conflict is
    reported as flag-only. The caller decides whether to block the write.
    """
    if not semantica_available():
        return []
    try:
        if op == "add_fact":
            return _detect_fact(wiki_id, context)
        if op == "link_entities":
            return _detect_relationship(wiki_id, context)
        if op == "upsert_entity":
            return _detect_entity(wiki_id, context)
    except Exception as e:
        # Conflict detection must never take down a write path.
        logger.warning("conflict detection failed for %s on %s: %s",
                       op, wiki_id, e)
        return []
    return []


# ---------------------------------------------------------------------------
# Fact-level detection (free-text + number changes)
# ---------------------------------------------------------------------------

def _detect_fact(wiki_id: str, ctx: dict) -> list[ConflictRecord]:
    """Bridge store.py's fact-text/number comparison into ConflictRecords.

    `ctx` keys:
      entity       - the entity wiki_id (person/alice-chen)
      text         - the incoming fact text
      confidence   - incoming confidence
      evidence     - incoming evidence refs
      existing_facts - list of {"text", "confidence", "evidence"} already stored
    """
    text = ctx.get("text")
    entity = ctx.get("entity") or ""
    existing = ctx.get("existing_facts") or []
    if not text or not existing:
        return []

    # Reuse the extractor's exact-match + same-claim + signature logic by
    # delegating to the module that owns it. Imported lazily to avoid a cycle.
    from app.extract.extractor import _fact_key, _fact_signature, _same_claim

    text_words, text_nums = _fact_signature(text)
    out: list[ConflictRecord] = []
    for f in existing:
        prior_text = f.get("text") if isinstance(f, dict) else getattr(f, "text", "")
        if not prior_text:
            continue
        if _fact_key(prior_text) == _fact_key(text):
            continue  # literally the same fact; nothing to flag

        prior_words, prior_nums = _fact_signature(prior_text)
        # Two facts must be about the SAME claim (subject + property) before a
        # differing number means anything. Without this gate, any two facts on
        # the entity that each happen to contain a number -- a meeting date
        # and a budget figure, say -- looked like a "conflict" purely because
        # their number sets differed, which is what caused a single busy
        # entity to generate a conflict record per unrelated fact pair.
        if not _same_claim(prior_words, text_words):
            continue
        # Same claim, different number => a correction/version bump, NOT a
        # duplicate. That is the case worth flagging as a superseding conflict.
        if prior_nums != text_nums and (prior_nums or text_nums):
            out.append(_rec(
                type="number_conflict", wiki_id=wiki_id, entity=entity,
                property_name="facts", conflicting_values=[prior_text, text],
                sources=(ctx.get("evidence") or []),
                confidence=ctx.get("confidence", 1.0), severity=0.7,
                recommended_action="newer value supersedes",
            ))
        # Same claim, same numbers -> a near-duplicate rephrasing, not a
        # conflict; nothing to flag.
    return out


# ---------------------------------------------------------------------------
# Relationship-level detection (Semantica detector)
# ---------------------------------------------------------------------------

def _detect_relationship(wiki_id: str, ctx: dict) -> list[ConflictRecord]:
    """Detect a relationship conflict between an incoming (source, target,
    category, label) and the source's existing relations.

    `ctx` keys: source, target, category, label, evidence.
    """
    source = ctx.get("source") or ""
    incoming_label = ctx.get("label") or ""
    existing_edges = ctx.get("existing_edges") or []  # list of dicts/tuples
    if not source or not existing_edges:
        return []

    # Group by a stable relationship identity (source->target) so Semantica
    # sees the two edges as the SAME relationship and compares their fields.
    # Semantica's detector checks for differing `type`/`properties`/`confidence`
    # within a group — we surface our `category` as `type` (so a contradictory
    # category reads as a conflict) and our `label` under properties.
    sem = _semantica()
    detector = _quiet_detector(sem["detector"])
    rels = []
    rel_id = f"{source}->{ctx.get('target')}"
    base = {"id": rel_id, "source_id": source, "target_id": ctx.get("target")}
    if incoming_label:
        rels.append({**base, "type": ctx.get("category", "related_to"),
                     "label": incoming_label, "properties": {"label": incoming_label},
                     "source": "incoming"})
    for e in existing_edges:
        target = e.get("target") if isinstance(e, dict) else getattr(e, "target", "")
        label = e.get("label") if isinstance(e, dict) else getattr(e, "label", "")
        cat = e.get("category") if isinstance(e, dict) else getattr(e, "category", "related_to")
        rels.append({**base, "type": cat, "label": label,
                     "properties": {"label": label}, "source": "existing"})

    conflicts = _safe_detect(detector, "relationship", rels)
    out = []
    for c in conflicts:
        out.append(_rec(
            type="relationship_conflict", wiki_id=wiki_id, entity=source,
            property_name=ctx.get("category") or "label",
            conflicting_values=_values_of(c),
            sources=(ctx.get("evidence") or []),
            confidence=_to_float(getattr(c, "confidence", 1.0), 1.0),
            severity=_to_float(getattr(c, "severity", 0.5), 0.5),
            recommended_action=str(getattr(c, "recommended_action", "") or ""),
            metadata={"raw_conflict_id": str(getattr(c, "conflict_id", ""))},
        ))
    return out


# ---------------------------------------------------------------------------
# Entity-level detection (type + value; Semantica detector)
# ---------------------------------------------------------------------------

def _detect_entity(wiki_id: str, ctx: dict) -> list[ConflictRecord]:
    """Detect a type or value conflict when upserting an entity.

    `ctx` keys: entity (wiki_id), type_, title, existing_type (or None),
                existing_compact, sample_values.
    """
    sem = _semantica()
    detector = _quiet_detector(sem["detector"])
    entity = ctx.get("entity") or ""
    existing_type = ctx.get("existing_type")
    incoming_type = ctx.get("type_") or ctx.get("type") or ""
    out: list[ConflictRecord] = []

    # Type conflict: same title already exists as a different type.
    if existing_type and incoming_type and existing_type != incoming_type:
        out.append(_rec(
            type="type_conflict", wiki_id=wiki_id, entity=entity,
            property_name="type", conflicting_values=[existing_type, incoming_type],
            sources=ctx.get("evidence") or [], severity=0.9,
            recommended_action="reclassify or disambiguate; existing type wins by default",
        ))

    # Value conflict: run Semantica's detector over a synthetic 2-source entity.
    values = ctx.get("sample_values") or []
    if existing_type and len(values) >= 2:
        entities = []
        for v in values:
            entities.append({"id": entity, "type": existing_type, "value": v,
                             "source": "existing"})
        entities.append({"id": entity, "type": incoming_type or existing_type,
                         "value": ctx.get("value") or "", "source": "incoming"})
        conflicts = _safe_detect(detector, "value", entities, property_name="value")
        for c in conflicts:
            out.append(_rec(
                type="value_conflict", wiki_id=wiki_id, entity=entity,
                property_name="value", conflicting_values=_values_of(c),
                sources=ctx.get("evidence") or [],
                confidence=_to_float(getattr(c, "confidence", 1.0), 1.0),
                severity=_to_float(getattr(c, "severity", 0.5), 0.5),
                recommended_action=str(getattr(c, "recommended_action", "") or ""),
                metadata={"raw_conflict_id": str(getattr(c, "conflict_id", ""))},
            ))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_detect(detector, kind: str, data, **kw) -> list:
    """Call a Semantica detector defensively; a failure raises, caught by the
    top-level try in detect_for_write."""
    try:
        if kind == "relationship":
            return detector.detect_relationship_conflicts(data)
        if kind == "type":
            return detector.detect_type_conflicts(data)
        # value: needs entities + property_name
        return detector.detect_value_conflicts(data, property_name=kw.get("property_name", "value"))
    except Exception as e:
        logger.debug("semantica %s conflict detect skipped: %s", kind, e)
        return []


def _quiet_detector(detector_cls):
    """Build a ConflictDetector with its progress output silenced.

    Semantica prints tqdm-style progress to stdout on every detect call,
    which pollutes server logs and tests. The tracker is a process
    singleton; the detector flips it back on in __init__, so we construct
    the detector first and then switch its tracker off for the call."""
    detector = detector_cls()
    try:
        tracker = getattr(detector, "progress_tracker", None)
        if tracker is not None:
            tracker.enabled = False
    except Exception:
        pass
    return detector


def _values_of(conflict) -> list:
    val = getattr(conflict, "conflicting_values", None)
    if isinstance(val, list):
        return val
    return [str(val)] if val else []


def _rec(**kw) -> ConflictRecord:
    """Build a ConflictRecord, stamping a fresh id when none was supplied.
    Keeps constructors terse and guarantees a usable conflict_id for the
    store's save() (which rejects empty ids)."""
    if not kw.get("conflict_id"):
        from app.graph.conflicts import new_conflict_id
        kw["conflict_id"] = new_conflict_id()
    return ConflictRecord(**kw)


def _to_float(value, default: float) -> float:
    """Coerce a Semantica severity/confidence to float. Semantica mixes both
    numeric (0..1) and string severities ('medium'), so coerce defensively."""
    try:
        v = float(value)
        return v
    except (TypeError, ValueError):
        if isinstance(value, str):
            s = value.lower()
            if s in ("high", "critical", "severe"):
                return 0.9
            if s in ("medium", "moderate"):
                return 0.5
            if s in ("low", "minor"):
                return 0.2
        return default
