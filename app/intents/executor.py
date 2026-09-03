"""LLM-issued CRUD intents against the entity graph.

WHY THIS EXISTS
    The chat endpoint is read-only by design: a model that writes straight into
    long-term memory mid-conversation is how memory fills with hallucinated
    facts. But the user explicitly wants the LLM to be able to READ, WRITE,
    UPDATE and DELETE on the graph -- full CRUD via the agent, not just the
    separate REST API.

    These two goals are reconciled the same way the extract pipeline does:
    the model emits EXPLICIT, STRUCTURED intents (JSON), and the backend
    executes them as discrete operations. Nothing is written implicitly. Each
    intent names exactly what to create/read/update/delete and where; the
    executor runs them through the SAME store API the REST layer uses, so the
    safety rails (permissions via wiki, validation of types/relations,
    audited through the ops log) all apply.

WHAT AN INTENT LOOKS LIKE
    chat/agent reply (markdown):

        <intents>
        [
          {"op":"add_fact","wiki":"q3-azure-migration","entity":"person/sarah-kim",
           "text":"Prefers Tuesdays for check-ins.","confidence":0.9},
          {"op":"update_fact","wiki":"q3-azure-migration","entity":"person/sarah-kim",
           "fact_id":"fact_0003","text":"Prefers Thursdays for check-ins."},
          {"op":"delete_entity","wiki":"q3-azure-migration","entity":"person/maria"}
        ]
        </intents>

    Supported ops: create_entity, update_entity, delete_entity, add_fact,
    update_fact, delete_fact, add_relation, update_relation, delete_relation,
    create_wiki.

EVERYTHING IS REPORTED BACK
    The executor returns one result per intent with status (applied | failed |
    skipped) and a message, so the LLM can confirm to the user exactly what
    changed and what did not. A failed intent (bad type, missing entity, bad
    relation category) fails that one intent only -- it never aborts the rest.
"""

from __future__ import annotations

import json
import logging
import re

from app.graph.store import (VALID_RELATION_CATEGORIES, EntityGraphStore)

logger = logging.getLogger("memory_backend.intents")

# Ops that need the agent to be a writer on the wiki. Reads (none of these
# mutate) can use read access; the endpoint gates per-intent.
VALID_OPS = {
    "create_entity", "update_entity", "delete_entity",
    "add_fact", "update_fact", "delete_fact",
    "add_relation", "update_relation", "delete_relation",
    "create_wiki",
}

# Ops that create or mutate entities/facts/relations (need write).
WRITE_OPS = VALID_OPS - {"create_wiki"}


class IntentError(ValueError):
    """A well-formed but unexecutable intent (missing field, bad type...)."""


def parse_intents(text: str) -> list[dict]:
    """Extract intent dicts from a model reply.

    Accepts a structured <intents> block OR a bare JSON array the model
    returned directly. Returns a list of intent dicts, or [] if none."""
    if not text:
        return []
    data = None
    m = re.search(r"<intents>\s*(\[.*?\])\s*</intents>", text, re.S | re.I)
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = None
    else:
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError:
            m2 = re.search(r"\[.*?\]", text, re.S)
            if m2:
                try:
                    data = json.loads(m2.group(0))
                except json.JSONDecodeError:
                    data = None
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def strip_intents(text: str) -> str:
    """Remove a model-emitted <intents>...</intents> block from a reply so it
    doesn't leak into the user-visible message. The block is parsed/executed
    before this is called, so dropping it from the displayed reply loses
    nothing.

    Robust to the model's sloppiness: handles optional whitespace/attributes
    on the tag, and an UNCLOSED block (the model sometimes drops </intents>)
    by stripping from <intents> to the end of the text."""
    if not text:
        return text
    out = re.sub(
        r"<intents\b[^>]*>(?:(?!</?intents\b).)*?(?:</intents>|$)", "",
        text, flags=re.S | re.I)
    return out.strip()


def execute_intents(store: EntityGraphStore, user_id: str,
                    intents: list[dict],
                    writer_for) -> list[dict]:
    """Run a list of intents against the store.

    `writer_for(wiki_id) -> bool` decides whether the agent may WRITE to a wiki
    (the endpoint passes a check against ROLE_WRITE). Reads are not separately
    executed here -- chat already injects memory as context. This executes the
    mutating ops.

    Returns a list of per-intent results:
        {"op", "wiki", "status": "applied"|"failed"|"skipped", "message", "detail"}
    """
    results: list[dict] = []
    for intent in intents:
        op = intent.get("op", "")
        wiki = intent.get("wiki") or intent.get("wiki_id") or user_id
        r = {"op": op, "wiki": wiki, "status": "failed",
             "message": "", "detail": ""}
        try:
            if op not in VALID_OPS:
                raise IntentError(f"unknown op {op!r}")
            if op in WRITE_OPS and not writer_for(wiki):
                raise IntentError(f"no write access to wiki {wiki!r}")
            _apply_one(store, user_id, wiki, op, intent, r)
        except IntentError as e:
            r["status"] = "skipped"
            r["message"] = str(e)
        except Exception as e:  # noqa: BLE001 - one bad intent must not stop the rest
            r["detail"] = f"{type(e).__name__}: {e}"
            r["message"] = f"failed: {e}"
            logger.warning("intent %s on %s failed: %s", op, wiki, e)
        results.append(r)
    # Ensure anything buffered lands (manifest/ops flush) for persistency.
    try:
        store.flush()
    except Exception:
        pass
    return results


def _apply_one(store: EntityGraphStore, user_id: str, wiki: str,
               op: str, intent: dict, r: dict) -> None:
    if op == "create_entity":
        type_ = intent.get("type")
        title = intent.get("title")
        if not type_ or not title:
            raise IntentError("create_entity needs 'type' and 'title'")
        entity = store.upsert_entity(
            wiki, type_, title,
            summary_append=intent.get("summary") or intent.get("compact"),
            aliases=intent.get("aliases"))
        r["status"] = "applied"
        r["message"] = f"entity {entity.wiki_id} created/updated"

    elif op == "update_entity":
        entity_id = intent.get("entity") or (intent.get("type") + "/" + intent.get("title", ""))
        if not entity_id:
            raise IntentError("update_entity needs 'entity' (e.g. person/sarah-kim)")
        existing = store.get_entity(wiki, entity_id, touch=False)
        if existing is None:
            raise IntentError(f"entity {entity_id!r} not found in {wiki!r}")
        summary = intent.get("summary")
        if summary:
            # Overwrite the summary (append would stack copies on every edit).
            existing.summary = summary.strip()
            existing.compact = summary.strip()
        if intent.get("aliases"):
            existing.aliases = sorted(set(existing.aliases) | set(intent["aliases"]))
        existing.metadata.updated_at = _now_iso()
        store.upsert_entity(
            wiki, existing.type, existing.title,
            aliases=existing.aliases, compact=existing.compact)
        r["status"] = "applied"
        r["message"] = f"entity {entity_id} updated"

    elif op == "delete_entity":
        entity_id = intent.get("entity") or (intent.get("type") + "/" + intent.get("title", ""))
        if not entity_id:
            raise IntentError("delete_entity needs 'entity'")
        ok = store.delete_entity(wiki, entity_id, cascade=bool(intent.get("cascade", True)),
                                 hard_delete=bool(intent.get("hard_delete", True)))
        r["status"] = "applied" if ok else "skipped"
        r["message"] = f"entity {entity_id} deleted" if ok else f"entity {entity_id} not found"

    elif op == "add_fact":
        entity_id = intent.get("entity")
        text = intent.get("text")
        if not entity_id or not text:
            raise IntentError("add_fact needs 'entity' and 'text'")
        entity = store.add_fact(
            wiki, entity_id, text,
            confidence=float(intent.get("confidence", 1.0)),
            evidence=intent.get("evidence"))
        new_id = entity.facts[-1].fact_id if entity.facts else None
        r["status"] = "applied"
        r["message"] = f"fact added to {entity_id}"
        r["detail"] = f"fact_id={new_id}"

    elif op == "update_fact":
        entity_id = intent.get("entity")
        fact_id = intent.get("fact_id")
        if not entity_id or not fact_id:
            raise IntentError("update_fact needs 'entity' and 'fact_id'")
        entity = store.update_fact(
            wiki, entity_id, fact_id,
            text=intent.get("text"),
            confidence=float(intent["confidence"]) if intent.get("confidence") is not None else None,
            evidence=intent.get("evidence"))
        r["status"] = "applied"
        r["message"] = f"fact {fact_id} updated on {entity_id}"

    elif op == "delete_fact":
        entity_id = intent.get("entity")
        fact_id = intent.get("fact_id")
        if not entity_id or not fact_id:
            raise IntentError("delete_fact needs 'entity' and 'fact_id'")
        store.remove_fact(wiki, entity_id, fact_id)
        r["status"] = "applied"
        r["message"] = f"fact {fact_id} deleted from {entity_id}"

    elif op == "add_relation":
        source = intent.get("source")
        target = intent.get("target")
        category = intent.get("category", "related_to")
        if not source or not target:
            raise IntentError("add_relation needs 'source' and 'target'")
        if category not in VALID_RELATION_CATEGORIES:
            raise IntentError(f"invalid relation category {category!r}")
        entity = store.link_entities(
            wiki, source, target, category=category,
            label=intent.get("label", ""), reason=intent.get("reason", ""),
            evidence=intent.get("evidence"),
            bidirectional=bool(intent.get("bidirectional", False)))
        new_id = entity.relations[-1].relation_id if entity.relations else None
        r["status"] = "applied"
        r["detail"] = f"relation_id={new_id}"
        r["message"] = f"relation {source}->{target} added"

    elif op == "update_relation":
        source = intent.get("source")
        relation_id = intent.get("relation_id")
        if not source or not relation_id:
            raise IntentError("update_relation needs 'source' and 'relation_id'")
        store.update_relation(
            wiki, source, relation_id,
            label=intent.get("label"),
            weight=float(intent["weight"]) if intent.get("weight") is not None else None,
            reason=intent.get("reason"),
            evidence=intent.get("evidence"))
        r["status"] = "applied"
        r["message"] = f"relation {relation_id} updated"

    elif op == "delete_relation":
        source = intent.get("source")
        relation_id = intent.get("relation_id")
        if not source or not relation_id:
            raise IntentError("delete_relation needs 'source' and 'relation_id'")
        store.remove_relation(wiki, source, relation_id,
                              bidirectional=bool(intent.get("bidirectional", False)))
        r["status"] = "applied"
        r["message"] = f"relation {relation_id} deleted"

    elif op == "create_wiki":
        # Creation + grant handled by the endpoint (needs registry/grant). The
        # executor only records it as available; the endpoint materialises it.
        raise IntentError("create_wiki must be handled by the router endpoint")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
