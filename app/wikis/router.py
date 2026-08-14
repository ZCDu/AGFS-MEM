"""
Choosing which wiki a conversation belongs to.

THE CASCADE
    Every wiki the user may reach is scored with the existing assessor, which
    matches text against entity titles, aliases and summaries. That costs no
    LLM call and no storage read beyond one registry object, so it runs on
    every turn without thought.

        clear winner            -> use it
        nothing scores          -> propose a new wiki (or ask)
        two close contenders    -> ask, or let a model break the tie

    A model is only consulted where the cheap signal is genuinely ambiguous.
    Routing every turn through an LLM would add a call and a few hundred
    milliseconds to answer a question deterministic matching already answers,
    and would make the same question route differently on consecutive turns —
    which is miserable to debug.

READS ROUTE, WRITES CONFIRM
    Routing a READ badly gives a worse answer. Routing a WRITE badly corrupts
    a graph, and nothing downstream detects it.

    So `route()` is free to pick for reads, and extraction is expected to show
    its chosen wiki for confirmation before applying. The asymmetry is
    deliberate: the cost of the two mistakes is not comparable.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from app.graph.store import EntityGraphStore
from app.verify.assessor import ConversationAssessor
from app.wikis.registry import (ROLE_READ, WikiMeta, WikiNameCollision,
                                WikiRegistry, slugify_wiki)

logger = logging.getLogger("memory_backend.router")

# Below this, a wiki is not a plausible home for the text.
MIN_SCORE = 0.15
# Two candidates within this of each other are a tie, not a winner.
TIE_MARGIN = 0.10


@dataclass
class WikiCandidate:
    wiki_id: str
    title: str
    score: float
    matched: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RouteDecision:
    action: str                 # "use" | "ambiguous" | "create" | "none"
    wiki_id: str | None = None
    candidates: list[WikiCandidate] = field(default_factory=list)
    proposed_title: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [c.to_dict() for c in self.candidates]
        return d


class WikiRouter:
    def __init__(self, registry: WikiRegistry, store: EntityGraphStore):
        self.registry = registry
        self.store = store

    def score(self, user_id: str, text: str,
              role: str = ROLE_READ) -> list[WikiCandidate]:
        """Rank the wikis this user may reach against the text.

        Scores only wikis the caller already has access to, so routing never
        reveals that a wiki exists by offering it as a destination.
        """
        candidates: list[WikiCandidate] = []
        for wiki in self.registry.list_for(user_id, role=role):
            try:
                assessment = ConversationAssessor(
                    self.store, min_words=1
                ).assess(wiki.wiki_id, text, link_only=True)
            except Exception:
                logger.warning("could not score wiki %s", wiki.wiki_id, exc_info=True)
                continue

            matched = [link.wiki_id for link in assessment.related[:5]]
            score = assessment.relevance

            # A registry-level fallback for a wiki whose entities do not match
            # but whose own title or description does. Without it, a brand new
            # wiki can never win a route, because it has no entities to match
            # and so can never acquire any.
            if score < MIN_SCORE:
                score = max(score, self._metadata_score(wiki, text))

            if score > 0:
                candidates.append(WikiCandidate(
                    wiki_id=wiki.wiki_id, title=wiki.title,
                    score=round(score, 3), matched=matched,
                    reason=(f"matched {', '.join(matched[:3])}" if matched
                            else "matched the wiki's own title or description")))

        return sorted(candidates, key=lambda c: -c.score)

    @staticmethod
    def _metadata_score(wiki: WikiMeta, text: str) -> float:
        """Overlap between the text and the wiki's title, description and
        sample entities. Weak by design — it exists to bootstrap an empty
        wiki, not to compete with real entity matches."""
        lowered = text.lower()
        hay = " ".join([wiki.title, wiki.description] + wiki.sample_entities).lower()
        words = {w for w in hay.split() if len(w) > 3}
        if not words:
            return 0.0
        hits = sum(1 for w in words if w in lowered)
        return min(0.4, hits / max(len(words), 1) * 2)

    def route(self, user_id: str, text: str, role: str = ROLE_READ,
              allow_create: bool = False) -> RouteDecision:
        """Decide where this text belongs.

        `allow_create` never creates anything here — it only lets the decision
        be "create". The caller performs it, so a proposal can be reviewed.
        """
        candidates = self.score(user_id, text, role=role)
        viable = [c for c in candidates if c.score >= MIN_SCORE]

        if not viable:
            if allow_create:
                title = self.propose_title(text)
                return RouteDecision(
                    action="create", candidates=candidates,
                    proposed_title=title,
                    reason="Nothing in reach matched this well enough to be its home.")
            return RouteDecision(
                action="none", candidates=candidates,
                reason="No accessible wiki matched. Name one explicitly, or "
                       "create one.")

        best = viable[0]
        runner_up = viable[1] if len(viable) > 1 else None
        if runner_up and (best.score - runner_up.score) < TIE_MARGIN:
            return RouteDecision(
                action="ambiguous", candidates=viable[:5],
                reason=f"{best.title} and {runner_up.title} scored within "
                       f"{TIE_MARGIN} of each other.")

        return RouteDecision(action="use", wiki_id=best.wiki_id,
                             candidates=viable[:5], reason=best.reason)

    @staticmethod
    def propose_title(text: str) -> str:
        """A first-guess name from the text's proper nouns.

        Deliberately crude. The proposal is meant to be reviewed, and a
        plausible-but-wrong name generated by a model is harder to notice as
        wrong than an obviously rough one.
        """
        import re
        from app.verify.assessor import _proper_noun_phrases
        # Speaker labels are transcript scaffolding, not proper nouns.
        # Without this a two-line "User: ... / Assistant: ..." transcript
        # proposed a wiki named "User Assistant Hello".
        role = re.compile(r"(?im)^\s*(?:user|assistant|you|bot|human|ai|用户|助手)\s*[:：]\s*")
        cleaned = role.sub("", text)
        # Contractions and the first-person pronoun are never proper nouns,
        # but the detector's regex captures them anyway because their leading
        # word is capitalised ("I'm", "It's", "We're", ...). Strip them before
        # detection so "I'm the Orion lead" proposes "Orion", not "I'm the
        # Orion".
        # Phones and smart-quote keyboards emit curly apostrophes (U+2019 /
        # U+2018) instead of ASCII ', and the contraction regex below only
        # matches ASCII, so "I'll" typed as "I\u2019ll" slipped through and was
        # proposed as a proper noun. Normalise them first.
        cleaned = cleaned.replace("\u2019", "'").replace("\u2018", "'")
        contraction = re.compile(
            r"\b(?:I|It|We|You|They|He|She|That|There|Here|What|Who|How)"
            r"'[a-z]{1,3}\b", re.I)
        cleaned = contraction.sub("", cleaned)
        # Reuse the assessor's detector, which strips sentence-initial capitals
        # ("Hello" and "How" are not entities just because they open a line).
        propers = _proper_noun_phrases(cleaned)
        # Dedup (a name said twice must not become "Orion Orion") and cap.
        seen: set[str] = set()
        out: list[str] = []
        for p in propers:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return " ".join(out[:3]) if out else "New Wiki"

    def create_from_decision(self, user_id: str, decision: RouteDecision,
                             title: str | None = None,
                             description: str = "") -> WikiMeta:
        """Perform a "create" decision.

        A name colliding with an existing wiki raises rather than creating a
        near-duplicate — the router should then use the existing one. Splitting
        a graph across "Sales" and "Sales Team" is worse than a large graph,
        because the connections that make it a graph fall across the split.
        """
        proposed = title or decision.proposed_title or "New Wiki"
        slug = slugify_wiki(proposed)
        if not slug:
            raise ValueError(f"Cannot derive a wiki id from {proposed!r}.")
        # allow_similar is not passed: a machine must not be the thing that
        # decides two similarly named worlds are distinct.
        return self.registry.create(proposed, created_by=user_id,
                                    description=description, wiki_id=slug)

    def route_or_existing(self, user_id: str, text: str) -> tuple[RouteDecision, WikiMeta | None]:
        """Route, creating when nothing fits — but reusing a near-duplicate.

        The convenience path for automatic ingestion. A name collision is
        resolved to the existing wiki rather than being an error, since the
        caller asked for somewhere to put this and there already is one.
        """
        decision = self.route(user_id, text, allow_create=True)
        if decision.action != "create":
            return decision, None
        try:
            wiki = self.create_from_decision(user_id, decision)
            decision.wiki_id = wiki.wiki_id
            decision.action = "use"
            decision.reason = f"Created {wiki.title!r}; nothing existing matched."
            return decision, wiki
        except WikiNameCollision as e:
            existing = self.registry.get(e.existing_id)
            decision.wiki_id = e.existing_id
            decision.action = "use"
            decision.reason = (f"Proposed name was too close to {e.existing_title!r}; "
                               f"used that instead of splitting the graph.")
            return decision, existing
