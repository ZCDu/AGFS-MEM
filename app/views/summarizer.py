"""LLM summarizer: turn a user's wiki into a filtered, citable digest.

The summarizer is the read-side counterpart to the reviewer/extractor: the
extractor writes raw facts into a wiki, and the summarizer synthesises them
for a *different* audience (a colleague or the boss). It never dumps the raw
graph by default -- it produces a structured digest with `sources` pointing
back at the entity wiki_ids, so a summary stays traceable.

Two products:
  - summarize(wiki, query, scope, viewer): an on-demand, query-bounded digest.
  - generate_diary(user, day): a scheduled chronological narrative of what the
    user did, stored under their home wiki for the day.

Both reuse the existing LLMClient (OpenAI-compatible chat completions). With
no LLM key configured they DEGRADE: summarize returns an error surfaced as
503 by the route, and the diary pass is skipped -- memory must not depend on
a third party being reachable.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from app.config import get_settings
from app.extract.llm import LLMClient, LLMNotConfigured, extract_json
from app.graph.keys import wiki_key
from app.graph.store import EntityGraphStore
from app.wikis.registry import WikiRegistry

logger = logging.getLogger("memory_backend.views")

_SUMMARY_SYSTEM = (
    "You summarise a person's private knowledge wiki into a clear, factual "
    "digest for a colleague or manager. The input is a JSON object listing "
    "knowledge-graph entities: each has a wiki_id, type, title, a compact "
    "one-line summary, and facts (statements with confidence). Some lines may "
    "be marked DELETED. Answer in STRICT JSON with exactly these keys and no "
    "prose outside it:\n"
    '{"summary": string, "key_people": [string], "decisions": [string], '
    '"open_items": [string], "timeline": [string], "sources": [string]}\n'
    "Rules:\n"
    "- summary: 3-8 sentence factual overview of what this wiki shows.\n"
    "- key_people: the person/org/project entities most central to it.\n"
    "- decisions: concrete decisions recorded, one sentence each.\n"
    "- open_items: unresolved items, or [] if there are none.\n"
    "- timeline: notable dated events; [] if none.\n"
    "- sources: the wiki_ids of the entities you actually used. This keeps the "
    "digest traceable -- every claim must be attributable.\n"
    "- Do NOT invent facts. Base every sentence on the entities given.\n"
    "- Ignore DELETED entities entirely.\n"
    "Write in the same language as the entities' content when it is not English."
)

_DIARY_SYSTEM = (
    "You write a short daily 'diary' entry describing what a person did on one "
    "day, based ONLY on facts recorded in their knowledge wiki that carry that "
    "day's evidence. The input is a JSON object: a list of entities, each with "
    "wiki_id, type, title, compact summary, and facts (each fact has text and "
    "optional evidence/date). Answer in STRICT JSON with exactly:\n"
    '{"diary": string, "highlights": [string], "topics_touched": [string], '
    '"sources": [string]}\n'
    "Rules:\n"
    "- diary: a 3-10 sentence first-then chronological narrative (\"Today they "
    "...\") of what happened, grounded only in the facts.\n"
    "- highlights: 1-3 short one-line takeaways most worth remembering.\n"
    "- topics_touched: short noun phrases for the subject areas this day's work "
    "belonged to.\n"
    "- sources: wiki_ids of entities used.\n"
    "- If the input has no dated facts, say that plainly rather than inventing.\n"
    "Write in the same language as the content."
)


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _flatten(store: EntityGraphStore, scope: str) -> list[dict]:
    """Load the active entities of one storage scope as plain dicts, trimmed
    to what a summarizer needs (no internal metadata fields)."""
    out: list[dict] = []
    try:
        entries = store.manifest.list_entries(scope)
    except Exception:
        return out
    by_id = {e.wiki_id: e for e in entries}
    for ent in store.list_entities(scope):
        e = store.get_entity(scope, ent, touch=False)
        if e is None or e.status == "deleted":
            continue
        facts = []
        for f in e.facts:
            facts.append({"text": f.text,
                          "confidence": f.confidence,
                          "evidence": list(f.evidence or []),
                          "created_at": f.created_at or ""})
        out.append({
            "wiki_id": e.wiki_id,
            "type": e.type,
            "title": e.title,
            "compact": e.compact or e.summary or "",
            "facts": facts,
        })
    return out


class WikiSummarizer:
    """Build filtered, LLM-summarized digests of a user's wiki/scopes."""

    def __init__(self, registry: WikiRegistry, store: EntityGraphStore,
                 llm: LLMClient | None = None):
        self.registry = registry
        self.store = store
        self.llm = llm or self._default_llm()

    @staticmethod
    def _default_llm() -> LLMClient:
        s = get_settings()
        return LLMClient(s.llm_api_key, s.llm_base_url, s.llm_model,
                         timeout=s.llm_timeout_seconds,
                         max_retries=2)

    def _scopes_for(self, owner: str, scope: str) -> list[str]:
        """Storage scopes to read for a requested view scope:
        \"all\" -> the home wiki + every topic sub-scope;
        \"topic:{key}\" -> just that sub-scope's composite scope;
        \"home\" -> just the home wiki page itself.
        """
        home = self.registry.get(owner)
        if scope == "topic:":
            scope = "all"
        if scope == "home":
            return [owner] if home is not None else []
        if scope.startswith("topic:"):
            key = scope[len("topic:"):]
            t = self.registry.get_topic(owner, key)
            if t is None:
                return []
            return [self.registry.topic_scope(owner, key)]
        # "all" -> home + every topic sub-scope
        scopes = [owner] if home is not None else []
        for t in self.registry.list_topics(owner):
            scopes.append(self.registry.topic_scope(owner, t.topic_key))
        return scopes

    def summarize(self, owner: str, query: str = "",
                  scope: str = "all", viewer: str = "admin",
                  is_admin: bool = False) -> dict:
        """Produce a summarized digest of `owner`'s wiki for `viewer`.

        The `query` bounds what is relevant ("what did X do last week?",
        \"summarise the Snowflake project\"). Scope \u2260 access: the caller's
        authority is checked by the ROUTE via registry.can_view(); this method
        assumes it has already been granted and only does the reading + LLM.
        Returns a dict with the digest fields; raises LLMNotConfigured if no
        key (the route surfaces 503).
        """
        scopes = self._scopes_for(owner, scope)
        entities: list[dict] = []
        for sc in scopes:
            entities.extend(_flatten(self.store, sc))
        if not entities:
            return {
                "summary": f"No stored information yet for {owner!r} "
                           f"(scope={scope!r}).",
                "key_people": [], "decisions": [], "open_items": [],
                "timeline": [], "sources": [], "query": query,
                "scope": scope, "owner": owner, "viewer": viewer,
                "entity_count": 0,
            }
        # Trim to a bounded, representative subset so the prompt stays
        # manageable on a large graph (and costs stay sane). Kept to the
        # highest-significance entities by a cheap heuristic (fact count).
        entities.sort(key=lambda e: -len(e.get("facts", [])))
        entities = entities[:60]
        payload = json.dumps({"entities": entities}, ensure_ascii=False)
        user = (
            f"Scope requested: {scope!r}.\n"
            f"Viewer's query (may be empty): {query or '(no query)'}\n\n"
            f"Entities:\n{payload}"
        )
        raw = self.llm.complete(self._summary_system(owner, viewer), user,
                                json_mode=True, max_tokens=3000)
        data = extract_json(raw)
        return {
            "summary": data.get("summary", ""),
            "key_people": data.get("key_people", []),
            "decisions": data.get("decisions", []),
            "open_items": data.get("open_items", []),
            "timeline": data.get("timeline", []),
            "sources": data.get("sources", []),
            "query": query, "scope": scope, "owner": owner, "viewer": viewer,
            "entity_count": len(entities),
            "generated_at": _today_utc(),
        }

    @staticmethod
    def _summary_system(owner: str, viewer: str) -> str:
        base = _SUMMARY_SYSTEM
        return (base +
                f"\nYou are summarising {owner!r}'s wiki for {viewer!r}.")

    def generate_diary(self, owner: str, day: str | None = None,
                       viewer: str = "system") -> dict | None:
        """Generate + persist a daily diary entry for `owner`.

        Reads the day's facts across the owner's home + topic scopes, asks the
        LLM for a diary, and stores it under the owner's wiki as
        `_diary/{day}.json` (visible to the owner). Returns the
        diary dict, or None when there was nothing dated / the LLM is down
        (caller should skip, not fail). Transparent by construction: it is
        stored in the owner's own wiki.
        """
        day = day or _today_utc()
        scopes = self._scopes_for(owner, "all")
        entities: list[dict] = []
        dated_hits = 0
        for sc in scopes:
            for e in _flatten(self.store, sc):
                has_day = any(
                    f.get("created_at", "").startswith(day)
                    or day in str(f.get("evidence", ""))
                    for f in e["facts"])
                if has_day:
                    dated_hits += 1
                entities.append(e)
        if dated_hits == 0 or not entities:
            return None  # nothing to write a diary about
        entities = entities[:60]
        payload = json.dumps({"day": day, "entities": entities},
                             ensure_ascii=False)
        user = f"Day: {day}\n\nEntities:\n{payload}"
        try:
            raw = self.llm.complete(_DIARY_SYSTEM, user, json_mode=True,
                                    max_tokens=2500)
            data = extract_json(raw)
        except LLMNotConfigured:
            raise
        except Exception as e:
            logger.warning("diary generation failed for %s on %s: %s",
                           owner, day, e)
            return None
        record = {
            "day": day, "owner": owner, "viewer": viewer,
            "entity_count": len(entities), "dated_facts": dated_hits,
            "diary": data.get("diary", ""),
            "highlights": data.get("highlights", []),
            "topics_touched": data.get("topics_touched", []),
            "sources": data.get("sources", []),
            "generated_at": _today_utc(),
        }
        self._store_diary(owner, day, record)
        return record

    def _store_diary(self, owner: str, day: str, record: dict) -> None:
        """Persist a diary entry under the owner's home wiki so it is visible
        to the owner in their UI. Namespaced by day; a re-run overwrites the
        same day (capture is idempotent)."""
        try:
            import json as _json
            key = wiki_key(owner, "_diary", f"{day}.json")
            self.registry.backend.put_bytes(
                key, _json.dumps(record, ensure_ascii=False).encode("utf-8"))
        except Exception:
            logger.debug("could not persist diary for %s on %s", owner, day,
                         exc_info=True)

    def read_diary(self, owner: str, day: str | None = None) -> list[dict]:
        """Read a user's stored diary entries (newest first). day=None -> all."""
        try:
            prefix = wiki_key(owner, "_diary/")
            keys = [k for k in self.registry.backend.list_keys(prefix)
                    if k.endswith(".json")]
            keys.sort(reverse=True)
            if day:
                keys = [k for k in keys if k.endswith(f"/{day}.json")]
            out = []
            for k in keys:
                raw = self.registry.backend.get_bytes(k)
                if raw:
                    out.append(json.loads(raw.data.decode("utf-8")))
            return out
        except Exception:
            logger.debug("could not read diary for %s", owner, exc_info=True)
            return []

    # ---------- transparency: the audit trail for summarized views ----------

    def record_view(self, owner: str, viewer: str, scope: str,
                    kind: str = "summary", note: str = "") -> None:
        """Write a transparent audit entry into the owner's home wiki.

        This is what makes oversight visible (decision a-i): whenever a
        colleague or admin summarises (or reads+summarises) a user's wiki, the
        owner can see it in their own UI. Best-effort; never raises.
        """
        try:
            entry = {
                "at": datetime.now(timezone.utc).isoformat(),
                "viewer": viewer, "kind": kind, "scope": scope, "note": note,
            }
            key = wiki_key(owner, "_views", "audit.jsonl")
            existing = b""
            raw = self.registry.backend.get_bytes(key)
            if raw:
                existing = raw.data
            line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            self.registry.backend.put_bytes(key, existing + line)
        except Exception:
            logger.debug("could not record view of %s by %s", owner, viewer,
                         exc_info=True)

    def list_audit(self, owner: str, limit: int = 50) -> list[dict]:
        """The audit trail of who viewed/summarised `owner`'s wiki, newest
        first, for surfacing in the owner's UI."""
        try:
            key = wiki_key(owner, "_views", "audit.jsonl")
            raw = self.registry.backend.get_bytes(key)
            if not raw:
                return []
            lines = raw.data.decode("utf-8").strip().splitlines()
            out = [json.loads(l) for l in lines if l.strip()]
            out.sort(key=lambda e: e.get("at", ""), reverse=True)
            return out[:limit]
        except Exception:
            logger.debug("could not read view audit for %s", owner, exc_info=True)
            return []
