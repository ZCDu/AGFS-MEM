"""AI-structure + Semantica conflict-resolve: the proper replacement for the
heuristic retraction detector.

ARCHITECTURE (matches how Semantica is designed to be used)
    Semantica's conflict engine is DETERMINISTIC and STRUCTURAL: it compares
    exact values of a property across sources of an entity. It does NOT read
    free-text prose. So the LLM's job is to STRUCTURE the user's claim into a
    Semantica-readable form (entity + property + value), and Semantica's job
    is to DETECT and RESOLVE the contradiction.

    user says:  "Sarah no longer leads the Azure migration"
      |  LLM structures it                          (semantica_crud.structure_claim)
      v
    claim = {entity:"person/sarah", property:"role", value:"no longer leads"} 
      |  Semantica ConflictDetector compares vs stored facts
      v
    Conflict (MOST_RECENT) -> resolved: new value supersedes
      |  execute update/delete on the store
      v
    memory updated

WHY THIS BEATS KEYWORD HEURISTICS
    A regex detector only catches trigger phrases ("no longer", "wrong").
    Structuring the claim means the CONTRADICTION is judged by Semantica on
    the VALUES, so phrasings a regex would miss ("Sarah handed the migration
    to David" -> role changes) are caught, because the extracted role value
    differs from the stored one.

SEMANTICA-READABLE SHAPE
    Semantica detects conflicts on a single `property` across records of one
    `entity`. We map:
      - entity   = the graph entity id (person/sarah-kim)
      - property = the fact's property bucket (default "facts"; can be role,
                   deadline, etc. if the strucurer chooses)
      - value    = the fact text (or a structured value when known)
    sources carry confidence + a monotonic timestamp so MOST_RECENT decides
    the incoming value wins on a correction.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.graph.store import EntityGraphStore
from app.intents.executor import execute_intents

logger = logging.getLogger("memory_backend.semantica_crud")

# How the LLM turns a free-text message into structured claims.
STRUCTURE_PROMPT = """\
You turn a user's statement about their world into STRUCTURED CLAIMS so a \
conflict-detection engine can compare them against existing memory.

The user said: "{message}"

Relevant existing memory for entities mentioned:
{context}

Return a JSON array of claim objects. Each claim is:
{{"entity":"<graph entity id, e.g. person/sarah-kim>",
  "property":"<the aspect being stated: role | deadline | contact | status | fact | ...>",
  "value":"<the concrete value, or null if the user is retracting/removing it>"}}

Rules:
- entity MUST be the stored entity id if one is in context; otherwise use the \
type prefix you think fits, e.g. "person/sarah-kim".
- If the user CORRECTS or CHANGES something, emit the NEW value (e.g. change \
of lead -> role with the new person). If the user only RETRACTS/removes \
something without a replacement, emit value=null for that property.
- If the user is merely asking a question or chatting (no change to memory), \
return [].
- Only include claims directly implied. Do not invent facts.
"""


class SemanticaConflictError(RuntimeError):
    pass


def structure_claim(message: str, context: str, llm,
                    retraction_hint: bool = False,
                    new_topic_hint: bool = False) -> list[dict]:
    """LLM structures a user statement into Semantica-readable claims.

    When `retraction_hint` is True, the message has already been classified as
    a retraction/correction by the deterministic detector, so the LLM is told
    it MUST produce a claim (value null for a pure retraction) instead of
    being free to return [] -- a model often rationalizes "I shouldn't have
    said that" as non-actionable and emits nothing.
    """
    from app.intents.executor import parse_intents
    extra = ""
    if retraction_hint:
        extra = ("\nThis message IS a retraction/correction of stored memory. "
                 "You MUST produce a claim for the affected entity. If the user "
                 "retracts something without a replacement, emit value=null. Do "
                 "NOT return [].")
    if new_topic_hint:
        # Brand-new topic: these are NEW entities the wiki does not have yet,
        # so a human-readable display title is needed for entity creation
        # (the entity id slug alone is not a good title).
        extra += ("\nThese entities are NEW to memory (a fresh wiki topic). For "
                  "EACH claim you MUST also include the human-readable display "
                  "title: "
                  "{\"entity\":\"<type>/<slug>\",\"title\":\"<display title>\","
                  "\"property\":\"<aspect>\",\"value\":\"<value>\"}. "
                  "The \"title\" should be the exact name as the user wrote it "
                  "(e.g. \"Wren & Co\", not \"wren-and-co\").")
    prompt = STRUCTURE_PROMPT.format(message=message, context=context or "(none in scope)") + extra
    try:
        raw = llm.complete(prompt, "", json_mode=False, max_tokens=800)
    except Exception as e:
        logger.warning("claim structuring failed: %s", e)
        return []
    return parse_intents(raw)


def claims_to_intents(claims: list[dict]) -> list[dict]:
    """Turn structured claims (optionally with a display 'title') into
    create_entity + add_fact intents that `execute_intents` can run.

    Used for NEW-topic capture where the entities do not exist yet:
    structure_claim() returns property-claims, but apply_semantica_conflict()
    can only touch entities that already exist (it issues add_fact, which
    refuses to create a node). For a fresh wiki we instead create each
    referenced entity first, then attach the stated facts.

    Claims:
      {'entity': 'organization/wren-and-co', 'property': 'type', 'value': 'law firm'}
      {'entity': 'person/felix-ashworth', 'property': 'role', 'value': 'senior partner'}
    Optionally: {'entity': ..., 'title': 'Wren & Co', ...}

    Returns a list of intent dicts: one create_entity per distinct entity,
    then one add_fact per claim. Empty if there is nothing actionable.

    IMPORTANT: the add_fact entity id is derived from the TYPE + slugified
    DISPLAY TITLE (exactly how upsert_entity stores it), never from the LLM's
    guessed slug. A claim id like 'organization/wren-and-co' for title 'Wren
    & Co' would slugify to 'wren-co' once created, so facts must reference
    'organization/wren-co' or they hit a nonexistent entity.
    """
    import re
    VALID_ENTITY_TYPES = {"person", "organization", "project", "event",
                          "concept", "artifact", "preference", "decision"}

    def _slug(name: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        return s or "entity"

    intents: list[dict] = []
    entities: dict[str, dict] = {}  # canonical entity_id -> {type, title}

    for c in claims or []:
        if not isinstance(c, dict):
            continue
        if c.get("value") in (None, "", "null"):
            continue  # pure retraction, nothing to create
        title = (c.get("title") or "").strip()
        ent_id = (c.get("entity") or "").strip()
        if not title and ent_id and "/" in ent_id:
            # No display title: fall back to the claim's own type + slug, but
            # re-slug the tail so it matches what creation will store.
            etype, tail = ent_id.split("/", 1)
            if etype in VALID_ENTITY_TYPES:
                title = tail
        if not title:
            continue
        # Infer type from the claim id when present, else skip (no type to
        # create under).
        etype = None
        if ent_id and "/" in ent_id:
            cand = ent_id.split("/", 1)[0]
            if cand in VALID_ENTITY_TYPES:
                etype = cand
        if not etype:
            continue
        canonical = f"{etype}/{_slug(title)}"
        entities.setdefault(canonical, {"type": etype, "title": title})

    for canonical, meta in entities.items():
        intents.append({
            "op": "create_entity", "type": meta["type"], "title": meta["title"],
        })

    # Facts reference the canonical (stored) entity id derived from the title.
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        if c.get("value") in (None, "", "null"):
            continue
        if not c.get("title"):
            continue
        etype = None
        ent_id = (c.get("entity") or "").strip()
        if ent_id and "/" in ent_id and ent_id.split("/", 1)[0] in VALID_ENTITY_TYPES:
            etype = ent_id.split("/", 1)[0]
        if not etype:
            continue
        text = str(c.get("value")).strip()
        if not text:
            continue
        canonical = f"{etype}/{_slug(str(c.get('title')).strip())}"
        intents.append({
            "op": "add_fact", "entity": canonical,
            "text": f"{c.get('property', 'fact')}: {text}",
        })
    return intents


def _semantica():
    from semantica.conflicts import ConflictDetector, ConflictResolver
    from semantica.conflicts.conflict_resolver import ResolutionStrategy
    return ConflictDetector, ConflictResolver, ResolutionStrategy


def _to_store_op(store, wiki, claim, resolved_value):
    """Given a resolved winner, produce the store intent that makes memory
    match the user's correction/retraction.

    resolved_value is the value Semantica chose as the truth. The incoming
    value always carries the newest timestamp, so MOST_RECENT picks it. We then
    decide update vs delete:
      - incoming value is null/empty  -> the user RETRACTED the fact -> delete
      - incoming value replaces old   -> the user CORRECTED it -> update (or
        add_fact if we do not know a fact_id yet)
    """
    entity = claim.get("entity") or ""
    incoming = claim.get("value")
    if not entity:
        return None

    stored = store.get_entity(wiki, entity, touch=False)
    if stored is None:
        # Entity doesn't exist. If the user stated a new fact, add it; if it
        # was a retraction of nothing, nothing to do.
        if incoming is None:
            return None
        return {"op": "add_fact", "wiki": wiki, "entity": entity,
                "text": str(incoming), "confidence": 0.9}

    # Find the best-matching stored fact to update/delete.
    target_fact = _find_most_similar_fact(stored.facts, claim, incoming)
    if incoming is None or (isinstance(incoming, str) and not incoming.strip()):
        # PURE RETRACTION. Distinguish two cases by whether the claim targets
        # a SPECIFIC attribute:
        #   * "X no longer <role/deadline/status>" -> property = role/deadline
        #     -> retract just that fact, KEEP the entity node.
        #   * "Forget X" (no specific attribute, generic property like 'fact'
        #     or missing) -> the user wants the entity GONE -> delete the node
        #     (and its links) so it truly vanishes.
        prop = (claim.get("property") or "").strip().lower()
        generic = prop in ("", "fact", "facts", "info", "text", "note", "summary")
        if not generic and target_fact is not None:
            return {"op": "delete_fact", "wiki": wiki, "entity": entity,
                    "fact_id": target_fact.fact_id}
        return {"op": "delete_entity", "wiki": wiki, "entity": entity}

    # Correction -> update the matching fact, or add a new one.
    if target_fact is not None:
        return {"op": "update_fact", "wiki": wiki, "entity": entity,
                "fact_id": target_fact.fact_id, "text": str(incoming),
                "confidence": 0.9}
    return {"op": "add_fact", "wiki": wiki, "entity": entity,
            "text": str(incoming), "confidence": 0.9}


def _find_most_similar_fact(facts, claim, incoming):
    """Locate the stored fact this claim is about, by property or by value
    overlap. Best-effort; None if no good match (caller falls back to add)."""
    prop = (claim.get("property") or "").lower()
    if not facts:
        return None
    # 1) Exact value match to the stored fact (correcting an existing fact).
    incoming_s = (str(incoming) if incoming is not None else "").lower().strip()
    for f in facts:
        if incoming_s and incoming_s in (f.text or "").lower():
            return f
    # 2) If the claim names a property and a fact starts near it, pick first.
    for f in facts:
        if prop and prop in (f.text or "").lower():
            return f
    # 3) Fall back to the most recently added fact (corrections usually target
    #    the latest note).
    return facts[-1]


def resolve_entity_id(store: EntityGraphStore, wiki: str, entity_id: str) -> str:
    """Map a claim's entity id to the ACTUAL stored entity id in the wiki (if
    any). The LLM often writes 'project/azure-migration' when the stored id
    is 'project/q3-azure-migration'; without this, a claim compares against
    an entity that holds no facts and nothing is ever detected/matched.

    Pure/read-only: never writes. Shared by `apply_semantica_conflict()` and
    by `ConversationExtractor._plan_removal()`, which needs the same lookup
    while still building a PLAN (no writes until `apply()` runs)."""
    if not entity_id:
        return ""
    if store.get_entity(wiki, entity_id, touch=False) is not None:
        return entity_id

    def _norm(s):
        try:
            from app.graph.title_resolver import _normalize
            return _normalize(s)
        except Exception:
            return s.lower().strip()

    key = _norm(entity_id.rsplit("/", 1)[-1])
    best, best_len = None, 0
    for eid in store.list_entities(wiki):
        ent = store.get_entity(wiki, eid, touch=False)
        if ent is None:
            continue
        names = [ent.title] + list(ent.aliases or []) + [eid.rsplit("/", 1)[-1]]
        for n in names:
            nn = _norm(n)
            if nn == key:
                return eid
            if key and (key in nn or nn in key) and len(nn) > best_len:
                best, best_len = eid, len(nn)
    return best or ""


def stage_pending_deletion(store: EntityGraphStore, user_id: str, wiki: str,
                           entity: str, op: str,
                           fact_id: str | None = None) -> tuple[str, dict]:
    """Public wrapper around the pending-deletion staging used by
    `apply_semantica_conflict`'s `confirm_deletes` path, so other callers
    (the extraction plan/apply path) can stage the exact same kind of
    reviewable deletion instead of executing it outright. Returns
    (pending_id, plan_dict) -- the plan dict is what a confirmation prompt
    shows the user."""
    plan = _describe_deletion(store, wiki, entity)
    pending_id = _stage_pending_deletion(store.backend, user_id, wiki, plan,
                                         op=op, fact_id=fact_id)
    return pending_id, plan


def apply_semantica_conflict(store: EntityGraphStore, user_id: str,
                             claims: list[dict], writer_for,
                             wiki_id: str | None = None,
                             confirm_deletes: bool = False) -> list[dict]:
    """Run Semantica's conflict detection over structured claims, record any
    detected contradiction, and apply the user's correction/retraction.

    Semantica's role here is DETECTION + AUDIT (its real strength): it
    compares the structured claim against the entity's stored facts and, when
    they contradict, that is recorded as a conflict (via the store's
    ConflictStore). The resolution is straightforwardly "the user's latest
    statement wins" -- which is what a correction/retraction means -- and the
    resulting store op (update/delete/add) is applied deterministically.

    `writer_for(wiki_id) -> bool` gates write access, same as execute_intents.

    When `confirm_deletes` is True, any operation that would DELETE a whole
    entity node (a pure "forget X") is NOT executed — it is staged as a
    pending deletion and the result carries `status:"needs_confirmation"`
    plus a `plan` describing the node + relations that would be removed. The
    caller must surface this to the user and call confirm_pending_deletion()
    to actually delete.
    """
    if not claims:
        return []
    detector = _quiet_detector()
    results: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for raw_claim in claims:
        wiki = raw_claim.get("wiki") or wiki_id or user_id
        raw_entity = raw_claim.get("entity") or ""
        entity = resolve_entity_id(store, wiki, raw_entity) or raw_entity
        claim = {**raw_claim, "entity": entity}
        r = {"op": "semantica_conflict", "wiki": wiki, "entity": entity,
             "status": "failed", "message": ""}
        try:
            if not writer_for(wiki):
                r["status"] = "skipped"
                r["message"] = f"no write access to wiki {wiki!r}"
                results.append(r)
                continue

            stored = store.get_entity(wiki, entity, touch=False)
            value = claim.get("value")
            value_s = None if value is None else str(value)
            prop = claim.get("property") or "fact"

            # Semantica detection: compare the structured claim against stored
            # facts (same entity + property). If a contradiction exists, record
            # it as a conflict for audit, then apply the user's newest statement.
            detected = False
            if stored is not None and stored.facts:
                records = []
                for f in stored.facts:
                    records.append({
                        "id": entity, "type": stored.type, prop: (f.text or ""),
                        "source": "stored", "confidence": 0.8,
                        "metadata": {"timestamp": _fact_ts(f)},
                    })
                records.append({
                    "id": entity, "type": stored.type, prop: value_s or "",
                    "source": "incoming", "confidence": 1.0,
                    "metadata": {"timestamp": now},
                })
                try:
                    conflicts = detector.detect_value_conflicts(records, property_name=prop)
                    if conflicts:
                        detected = True
                        _record_conflict(store, wiki, entity, prop, conflicts, value_s)
                except Exception:
                    logger.debug("semantica value-conflict detect skipped", exc_info=True)

            # Apply the user's correction/retraction (newest wins).
            op = _to_store_op(store, wiki, claim, None)
            if op is None:
                r["status"] = "skipped"
                r["message"] = "nothing to change"
            elif op["op"] in ("delete_entity", "delete_fact") and confirm_deletes:
                # ANY destructive delete (a pure "forget X" node removal, or a
                # specific fact retraction) is staged, never auto-applied. We
                # ask for explicit confirmation, returning exactly what would
                # be deleted so the caller (e.g. the webapp) can show an
                # "are you sure?" prompt. Nothing is deleted here.
                plan = _describe_deletion(store, wiki, op["entity"])
                pending_id = _stage_pending_deletion(
                    store.backend, user_id, wiki, plan,
                    op=op["op"],
                    fact_id=op.get("fact_id"))
                r.update({
                    "status": "needs_confirmation",
                    "pending_id": pending_id,
                    "plan": plan,
                    "message": f"Confirm deletion on node {op['entity']}: "
                               f"{op['op']}?",
                })
            else:
                res = execute_intents(store, user_id, [op], writer_for=writer_for)
                r.update(res[0])
                if detected:
                    r["message"] = "Semantica conflict detected + applied: " + r.get("message", "applied")
            results.append(r)
        except Exception as e:  # noqa: BLE001
            r["detail"] = f"{type(e).__name__}: {e}"
            r["message"] = f"semantica conflict failed: {e}"
            logger.warning("semantica conflict apply failed for %s: %s", entity, e)
            results.append(r)
    try:
        store.flush()
    except Exception:
        pass
    return results


def _quiet_detector():
    """Semantica ConflictDetector with progress output silenced."""
    from semantica.conflicts import ConflictDetector
    d = ConflictDetector()
    try:
        d.progress_tracker.enabled = False
    except Exception:
        pass
    return d


def _record_conflict(store, wiki, entity, prop, semantica_conflicts, incoming_value):
    """Persist a Semantica-detected contradiction as an OKF conflict record so
    it is auditable, using the store's ConflictStore. Best-effort."""
    try:
        from app.graph.conflicts import ConflictRecord, new_conflict_id
        for c in semantica_conflicts[:5]:
            rec = ConflictRecord(
                conflict_id=new_conflict_id(),
                type="value_conflict", wiki_id=wiki, entity=entity,
                property_name=prop,
                conflicting_values=list(getattr(c, "conflicting_values", None) or []),
                sources=[],
                severity=_to_severity(getattr(c, "severity", None)),
                recommended_action="latest statement wins",
                metadata={"semantica": True, "incoming_value": incoming_value},
            )
            try:
                store.conflicts.save(wiki, rec)
            except Exception:
                pass
    except Exception:
        logger.debug("could not record semantica conflict", exc_info=True)


def _to_severity(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        if isinstance(s, str):
            s = s.lower()
            if s in ("high", "critical"): return 0.9
            if s in ("medium", "moderate"): return 0.5
            if s in ("low", "minor"): return 0.2
        return 0.5


def _fact_ts(fact) -> str:
    """A monotonic-ish timestamp for a stored fact, so Semantica's MOST_RECENT
    ordering between stored facts and the incoming claim is deterministic."""
    return (getattr(fact, "created_at", None) or
            getattr(fact, "updated_at", None) or
            datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Pending destructive deletions (confirm-before-forget)
# ---------------------------------------------------------------------------
# A pure "forget X" deletes a whole entity node + its relations. That is
# destructive, so we stage it and require an explicit user confirmation before
# applying. Each staged deletion is a small JSON object under:
#   {user_id}/pending_deletions/{pending_id}.json
# {wiki_id, entity, entity_title, node_count, relations: [labels]}

import secrets as _secrets  # noqa: E402

def _pending_key(user_id: str, pending_id: str) -> str:
    return f"{user_id}/pending_deletions/{pending_id}.json"


def _describe_deletion(store, wiki: str, entity: str) -> dict:
    """What a node deletion would remove: the node + its inbound/outbound
    relations (their labels), so the confirmation prompt can list them."""
    ent = store.get_entity(wiki, entity, touch=False)
    relations = []
    if ent is not None:
        for rel in (ent.relations or []):
            label = getattr(rel, "label", None) or getattr(rel, "category", "related_to")
            other = getattr(rel, "target", None) or getattr(rel, "object", None) or "?"
            relations.append(f"{label} -> {other}")
    return {
        "wiki": wiki,
        "entity": entity,
        "entity_title": getattr(ent, "title", None) if ent else None,
        "node_count": 1 if ent is not None else 0,
        "relations": relations,
        "facts": len(getattr(ent, "facts", None) or []) if ent else 0,
    }


def _stage_pending_deletion(backend, user_id: str, wiki: str, plan: dict,
                            op: str = "delete_entity",
                            fact_id: str | None = None) -> str:
    """Persist a staged deletion and return its pending id.

    `op` names the operation to run on confirmation: delete_entity (whole
    node) or delete_fact (remove one stored fact, keyed by `fact_id`).
    """
    import json
    from datetime import datetime, timezone as _tz
    pending_id = _secrets.token_hex(8)
    record = {
        "pending_id": pending_id,
        "created_at": datetime.now(_tz.utc).isoformat(),
        "status": "awaiting_confirmation",
        "op": op,
        "fact_id": fact_id,
        **plan,
    }
    backend.put_bytes(_pending_key(user_id, pending_id),
                      json.dumps(record, ensure_ascii=False).encode("utf-8"))
    return pending_id


def list_pending_deletions(backend, user_id: str) -> list[dict]:
    """All staged, not-yet-confirmed destructive deletions for a user."""
    import json
    out = []
    for key in backend.list_keys(f"{user_id}/pending_deletions/"):
        if not key.endswith(".json"):
            continue
        raw = backend.get_bytes(key)
        if raw is None:
            continue
        try:
            d = json.loads(raw.data.decode("utf-8"))
            if d.get("status") == "awaiting_confirmation":
                out.append(d)
        except Exception:
            continue
    return out


def confirm_pending_deletion(store: EntityGraphStore, user_id: str, writer_for,
                             pending_id: str) -> dict:
    """Execute a staged node deletion after the user confirmed. Returns the
    outcome. Deletes the pending record regardless of outcome (one-shot)."""
    import json
    backend = store.backend
    raw = backend.get_bytes(_pending_key(user_id, pending_id))
    if raw is None:
        return {"status": "not_found", "pending_id": pending_id}
    try:
        rec = json.loads(raw.data.decode("utf-8"))
    except Exception:
        return {"status": "invalid", "pending_id": pending_id}
    wiki = rec.get("wiki")
    entity = rec.get("entity")
    # Which destructive op this pending record encodes (delete_entity for a
    # whole node, delete_fact for a single fact). Older staged records may
    # lack the op field; default to delete_entity for backward compatibility.
    op_type = rec.get("op") or "delete_entity"
    fact_id = rec.get("fact_id")
    # Best-effort: remove the pending record so it cannot be applied twice.
    try:
        backend.delete(_pending_key(user_id, pending_id))
    except Exception:
        pass
    if not wiki or not entity:
        return {"status": "invalid", "pending_id": pending_id}
    if not writer_for(wiki):
        return {"status": "no_write_access", "pending_id": pending_id, "wiki": wiki}
    op = {"op": op_type, "wiki": wiki, "entity": entity}
    if op_type == "delete_fact":
        if not fact_id:
            return {"status": "invalid", "pending_id": pending_id,
                    "message": "delete_fact pending record missing fact_id"}
        op["fact_id"] = fact_id
    try:
        res = execute_intents(store, user_id, [op], writer_for=writer_for)
        out = dict(res[0])
        out["pending_id"] = pending_id
        return out
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "pending_id": pending_id, "detail": str(e)}


def cancel_pending_deletion(backend, user_id: str, pending_id: str) -> dict:
    """Abandon a staged deletion WITHOUT executing it. The entity stays.

    One-shot: the pending record is removed whether or not it existed, so
    confirming then cancelling (or vice-versa) cannot double-apply.
    """
    raw = backend.get_bytes(_pending_key(user_id, pending_id))
    if raw is None:
        return {"status": "not_found", "pending_id": pending_id}
    try:
        backend.delete(_pending_key(user_id, pending_id))
    except Exception:
        pass
    return {"status": "cancelled", "pending_id": pending_id}
