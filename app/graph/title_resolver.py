"""
Wiki Title Resolver — PLAN.md §7.3.

Solves the dedup problem: without this, upsert_entity("person", "Alice")
and upsert_entity("person", "Alice Chen") create two separate entities for
what's probably the same person. This module decides, before anything gets
written, whether a candidate title is (a) an existing entity under a new
name — merge into it, adding the new title as an alias, (b) genuinely new —
create it, or (c) too ambiguous to decide automatically — hold it in
wiki/_inbox/ instead of guessing.

Matching strategy (deliberately simple — no embeddings/ML, matching the
Wiki store's design principle of staying explainable and debuggable):

  1. Exact match (case/whitespace-insensitive) against existing titles.
  2. Exact match against existing aliases.
  3. Word-containment: the candidate's words are a subset of an existing
     title's words or vice versa (catches "Alice" <-> "Alice Chen").
  4. Fuzzy string similarity (difflib) above a threshold, as a fallback
     for near-misses (typos, minor rewording) that containment doesn't
     catch.

If `type_hint` is given, matching is scoped to just that type — per
PLAN.md's priority order, a user-named/typed object is trusted, so we
don't second-guess it by matching across unrelated types. If no type_hint
is given, matching runs across all types, and a match spanning more than
one type is treated as ambiguous (inbox), since we have no signal to
prefer one over another.

Zero, one, or many candidates changes what happens:
  - Exactly one candidate  -> merge into it.
  - Zero candidates + type_hint given -> create new entity of that type.
  - Zero candidates + no type_hint -> can't classify -> inbox.
  - More than one candidate (ambiguous) -> inbox, regardless of type_hint.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.graph.manifest import ManifestEntry
from app.graph.store import VALID_TYPES, Entity, EntityGraphStore
from app.storage.backend import StorageBackend

FUZZY_THRESHOLD = 0.82  # difflib ratio; tune if this over/under-matches in practice


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s]", "", text)  # strip punctuation
    text = re.sub(r"\s+", " ", text)
    return text


def _word_containment(a: str, b: str) -> bool:
    """True if one's word-set is a subset of the other's (and they share
    at least one word) — catches "Alice" <-> "Alice Chen" without needing
    fuzzy matching, and without the false-positive risk of pure substring
    matching (e.g. "Art" inside "Artificial Intelligence")."""
    words_a, words_b = set(a.split()), set(b.split())
    if not words_a or not words_b:
        return False
    if words_a == words_b:
        return False  # exact match already handled separately
    return (words_a <= words_b or words_b <= words_a)


@dataclass
class ResolveResult:
    action: str  # "matched" | "created" | "inbox"
    wiki_id: str | None = None
    entity: Entity | None = None
    inbox_id: str | None = None
    reason: str = ""


@dataclass
class InboxCandidate:
    candidate_id: str
    title: str
    type_hint: str | None = None
    aliases: list[str] = field(default_factory=list)
    summary_append: str | None = None
    compact: str | None = None
    significance: float | None = None
    reason: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id, "title": self.title,
            "type_hint": self.type_hint, "aliases": self.aliases,
            "summary_append": self.summary_append, "compact": self.compact,
            "significance": self.significance, "reason": self.reason,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "InboxCandidate":
        return InboxCandidate(
            candidate_id=d["candidate_id"], title=d["title"],
            type_hint=d.get("type_hint"), aliases=list(d.get("aliases", [])),
            summary_append=d.get("summary_append"), compact=d.get("compact"),
            significance=d.get("significance"), reason=d.get("reason", ""),
            created_at=d.get("created_at", ""),
        )


class WikiInbox:
    """wiki/_inbox/{candidate_id}.json — holding area for candidates the
    resolver couldn't confidently classify. Plain JSON, not OKF (these
    aren't entities yet), no conditional-write concurrency needed since
    each candidate_id is unique at creation time."""

    def __init__(self, backend: StorageBackend):
        self.backend = backend
        self._counter = 0

    def _key(self, user_id: str, candidate_id: str) -> str:
        return f"{user_id}/wiki/_inbox/{candidate_id}.json"

    def _next_id(self) -> str:
        self._counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"inbox_{ts}_{self._counter}"

    def add(self, user_id: str, title: str, type_hint: str | None = None,
            aliases: list[str] | None = None, summary_append: str | None = None,
            compact: str | None = None, significance: float | None = None,
            reason: str = "") -> InboxCandidate:
        import json
        candidate = InboxCandidate(
            candidate_id=self._next_id(), title=title, type_hint=type_hint,
            aliases=list(aliases or []), summary_append=summary_append,
            compact=compact, significance=significance, reason=reason,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        self.backend.put_bytes(self._key(user_id, candidate.candidate_id), payload)
        return candidate

    def get(self, user_id: str, candidate_id: str) -> InboxCandidate | None:
        import json
        raw = self.backend.get_bytes(self._key(user_id, candidate_id))
        if raw is None:
            return None
        return InboxCandidate.from_dict(json.loads(raw.data.decode("utf-8")))

    def list(self, user_id: str) -> list[InboxCandidate]:
        import json
        keys = self.backend.list_keys(f"{user_id}/wiki/_inbox/")
        candidates = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            raw = self.backend.get_bytes(key)
            if raw is not None:
                candidates.append(InboxCandidate.from_dict(json.loads(raw.data.decode("utf-8"))))
        return candidates

    def remove(self, user_id: str, candidate_id: str) -> bool:
        key = self._key(user_id, candidate_id)
        existed = self.backend.exists(key)
        if existed:
            self.backend.delete(key)
        return existed


class WikiTitleResolver:
    def __init__(self, store: EntityGraphStore):
        self.store = store
        self.inbox = WikiInbox(store.backend)

    def _find_candidates(self, user_id: str, title: str,
                          type_hint: str | None) -> list[ManifestEntry]:
        entries = self.store.manifest.list_entries(user_id, type_filter=type_hint)
        entries = [e for e in entries if e.status != "deprecated"]
        norm_title = _normalize(title)

        exact = [e for e in entries if _normalize(e.title) == norm_title]
        if exact:
            return exact

        alias_matches = [
            e for e in entries if any(_normalize(a) == norm_title for a in e.aliases)
        ]
        if alias_matches:
            return alias_matches

        fuzzy: list[ManifestEntry] = []
        for e in entries:
            norm_existing = _normalize(e.title)
            if _word_containment(norm_title, norm_existing):
                fuzzy.append(e)
                continue
            ratio = difflib.SequenceMatcher(None, norm_title, norm_existing).ratio()
            if ratio >= FUZZY_THRESHOLD:
                fuzzy.append(e)
        return fuzzy

    def resolve(
        self,
        user_id: str,
        title: str,
        type_hint: str | None = None,
        aliases: list[str] | None = None,
        summary_append: str | None = None,
        compact: str | None = None,
        significance: float | None = None,
    ) -> ResolveResult:
        if type_hint is not None and type_hint not in VALID_TYPES:
            raise ValueError(f"Unknown type: {type_hint!r} (expected one of {VALID_TYPES})")

        candidates = self._find_candidates(user_id, title, type_hint)

        if len(candidates) == 1:
            match = candidates[0]
            merged_aliases = list(aliases or [])
            if _normalize(title) != _normalize(match.title):
                merged_aliases.append(title)  # the searched-for title becomes an alias
            entity = self.store.upsert_entity(
                user_id, match.type, match.title,
                aliases=merged_aliases, summary_append=summary_append,
                compact=compact, significance=significance,
            )
            return ResolveResult(
                action="matched", wiki_id=entity.wiki_id, entity=entity,
                reason=f"matched existing entity {match.wiki_id!r} (title/alias/similarity match)",
            )

        if len(candidates) > 1:
            candidate = self.inbox.add(
                user_id, title, type_hint=type_hint, aliases=aliases,
                summary_append=summary_append, compact=compact, significance=significance,
                reason=f"ambiguous: matched {len(candidates)} existing entities "
                       f"({', '.join(c.wiki_id for c in candidates)})",
            )
            return ResolveResult(action="inbox", inbox_id=candidate.candidate_id, reason=candidate.reason)

        # no candidates
        if type_hint is not None:
            entity = self.store.upsert_entity(
                user_id, type_hint, title, aliases=aliases,
                summary_append=summary_append, compact=compact, significance=significance,
            )
            return ResolveResult(
                action="created", wiki_id=entity.wiki_id, entity=entity,
                reason="no existing match; created with the given type",
            )

        candidate = self.inbox.add(
            user_id, title, type_hint=None, aliases=aliases,
            summary_append=summary_append, compact=compact, significance=significance,
            reason="no existing match and no type given; cannot classify automatically",
        )
        return ResolveResult(action="inbox", inbox_id=candidate.candidate_id, reason=candidate.reason)

    def resolve_inbox_candidate(
        self, user_id: str, candidate_id: str, type_: str, title: str | None = None,
    ) -> ResolveResult:
        """Manually resolve a held candidate: assign it a type (and
        optionally override its title), which runs it back through
        resolve() with that type_hint now fixed — so it still merges into
        an existing entity of that type if one matches, rather than
        blindly creating a duplicate."""
        candidate = self.inbox.get(user_id, candidate_id)
        if candidate is None:
            raise ValueError(f"Inbox candidate {candidate_id!r} not found")

        result = self.resolve(
            user_id, title or candidate.title, type_hint=type_,
            aliases=candidate.aliases, summary_append=candidate.summary_append,
            compact=candidate.compact, significance=candidate.significance,
        )
        self.inbox.remove(user_id, candidate_id)
        return result

    def discard_inbox_candidate(self, user_id: str, candidate_id: str) -> bool:
        return self.inbox.remove(user_id, candidate_id)
