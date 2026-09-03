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
from app.verify.assessor import (ConversationAssessor,
                                 _proper_noun_phrases)
from app.wikis.registry import (ROLE_READ, WikiMeta, WikiNameCollision,
                                WikiRegistry, slugify_wiki)

logger = logging.getLogger("memory_backend.router")


def _known_entity_names(candidate: "WikiCandidate") -> list[str]:
    """Names the candidate wiki already "knows": the entity ids it matched
    plus its own title. Used to detect whether the text introduces genuinely
    new named entities. The title matters because an EMPTY wiki has no
    entities to match — its title/description are the only thing it knows, so
    a first message about its own subject must not be treated as a new topic.
    Extracting proper nouns is cheap and needs no LLM, so this stays in the
    deterministic route path."""
    out = [m.rsplit("/", 1)[-1].replace("-", " ") for m in candidate.matched]
    out.append(candidate.title)
    return out

# Below this, a wiki is not a plausible home for the text.
MIN_SCORE = 0.15
# Below this score the match is THIN: it comes from a summary/topical
# Jaccard overlap or from the wiki's own title/description matching a few
# buzzwords, not from a specific named entity (a person, project, org) that
# the text is really about. Routing unrelated content into such a wiki is
# exactly the "unrelated nodes forced into a wiki page" failure. A text with
# no STRONG home should get its own wiki rather than being absorbed by a
# weak coincidence of words.
STRONG_SCORE = 0.65
# Two candidates within this of each other are a tie, not a winner.
TIE_MARGIN = 0.10

# Words that never contribute to a proper-noun phrase being a genuinely NEW
# named entity. "The Q3 Azure" is over-captured by the proper-noun detector;
# the meaningful signal is only "azure". Shouting these out stops an
# over-captured fragment from spuriously counting as topic discovery.
_NAMEPHRASE_STOPWORDS = frozenset("the a an of in on at to for with and or as by from q1 q2 q3 q4 month day week".split())


@dataclass
class WikiCandidate:
    wiki_id: str
    title: str
    score: float
    matched: list[str] = field(default_factory=list)
    reason: str = ""
    # True when the score reflects a specific named-entity match (title,
    # alias, or proper-noun), not just word-level overlap with a summary or
    # the wiki's metadata. Only a strong match justifies using the wiki as
    # the home for content; a thin match should not swallow a new topic.
    strong: bool = True
    # False when the candidate's relevance is driven purely by NAME mention
    # (entities whose titles/aliases the text cites) with NO summary-tier
    # topical overlap. Such a text REFERENCES the wiki's entities but its
    # own subject may be a different topic — e.g. "Alice Chen wants a new
    # espresso machine" cites person/alice-chen but is not about Alice's
    # work. A topical candidate shares content vocabulary via the summary
    # tier, so the text is (probably) genuinely about what the wiki knows.
    topical: bool = True
    # True when the text's subject shares at least one CURATED TAG with this
    # wiki's tags. A tag-aligned candidate is a plausible topic home; a
    # tag-mismatched one (even a strong name match) reads as the wrong topic -
    # the "skip and search other wikis" signal. False back-compat: a wiki with
    # NO tags yet is treated as tag-neutral (True) so an un-tagged wiki is not
    # spuriously skipped before its tags are populated.
    tags_match: bool = True
    # False when the text's subject matches the entities' summaries but NOT
    # the wiki's own identity (title / description / topic). A contaminated
    # wiki -- one that absorbed unrelated entities whose summaries now carry
    # a different topic -- matches incoming text on those misfit entities,
    # so `topical` stays True while `coherent` is False. This is the
    # self-reinforcing contamination trap: once an unrelated entity lives in
    # a wiki, the wiki keeps matching future text about that topic forever.
    # `coherent` breaks the trap (the write side routes create/query instead
    # of use). A genuine continuation overlaps the wiki's own topic identity,
    # so it stays coherent.
    coherent: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RouteDecision:
    action: str                 # "use" | "ambiguous" | "create" | "none" | "query"
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

            # Is the relevance grounded in what this text is actually ABOUT
            # (content-word overlap with what the wiki already knows about
            # the entities it cited), or only in NAME citations? Pure
            # title/alias mentions mean the text merely references the wiki's
            # entities and its own subject may be a different topic entirely.
            # We keep this on the candidate so route() can tell "this is the
            # home" from "this just cited one of our names".
            topical = self._has_content_overlap(wiki.wiki_id, text, matched)

            # A registry-level fallback for a wiki whose entities do not match
            # but whose own title or description does. Without it, a brand new
            # wiki can never win a route, because it has no entities to match
            # and so can never acquire any.
            if score < MIN_SCORE:
                score = max(score, self._metadata_score(wiki, text))

            # A summary/alias match at 0.7+ (proper noun, alias) is a real
            # reference to this wiki's content -> strong. Everything below
            # STRONG_SCORE came from the weak tiers (summary Jaccard overlap
            # or metadata buzzwords), so it cannot justify absorbing a topic
            # into this wiki on its own.
            strong = score >= STRONG_SCORE and bool(matched)

            # Does the text's subject actually belong to this wiki's TOPIC?
            # A contaminated wiki matches on misfit entities whose summaries
            # carry an unrelated topic (coherent False); a genuine
            # continuation's matched entities agree with the wiki identity
            # (coherent True). See _is_coherent.
            coherent = self._is_coherent(wiki, matched)

            # TAG GATE: does the text's key points share a curated tag with
            # this wiki? A wiki whose tags disagree with the subject is NOT a
            # home for it, no matter how well entity names happened to match -
            # the router should skip it and search the other wikis (or start a
            # new topic). A wiki with no tags yet is tag-neutral.
            tags_match = self._tags_match(wiki, text)

            if score > 0:
                candidates.append(WikiCandidate(
                    wiki_id=wiki.wiki_id, title=wiki.title,
                    score=round(score, 3), matched=matched, strong=strong,
                    topical=topical, coherent=coherent, tags_match=tags_match,
                    reason=(f"matched {', '.join(matched[:3])}" if matched
                            else "matched the wiki's own title or description")))

        return sorted(candidates, key=lambda c: -c.score)

    def _tags_match(self, wiki: WikiMeta, text: str) -> bool:
        """True when the text's key points share at least one curated tag with
        `wiki`. A wiki with no tags yet is tag-neutral (True) so un-tagged
        wikis are never spuriously skipped. Tag comparison is on normalized
        content words (case-insensitive, stopwords dropped), so a tag like
        "search-migration" and a message about "search migration" agree.
        """
        from app.verify.assessor import _content_tokens
        tag_words = self._normalize_tags(wiki.tags)
        if not tag_words:
            return True
        incoming = _content_tokens(text)
        if not incoming:
            return False
        return bool(incoming & tag_words)

    @staticmethod
    def _normalize_tags(tags: list[str]) -> set[str]:
        """Tags to a set of normalized content words for matching."""
        from app.verify.assessor import _content_tokens
        out: set[str] = set()
        for t in tags or []:
            out |= _content_tokens(str(t))
        return out

    def _has_content_overlap(self, scope: str, text: str,
                             matched: list[str]) -> bool:
        """True when the text shares content words with what the wiki already
        knows about the entities it cites (their compact summaries).

        This is the cheap test that separates "this text is about the same
        topic" from "this text merely cited one of our names". We deliberately
        do NOT reuse the assessor's `matched_on == "summary"` tier: that tier
        fires only above a strict 0.18 Jaccard, which real short summaries and
        short messages rarely clear, so relying on it would turn almost every
        name match into a query. A relaxed CONTAINMENT test (any substantive
        word the summary uses also appears in the text) is enough: "Orion
        latency is above target" shares "latency" with Orion's summary and is
        topical; "espresso machine in the break room" shares none of Alice's
        words and is not.
        """
        from app.verify.assessor import _content_tokens
        incoming = _content_tokens(text)
        if not incoming:
            return False
        try:
            entries = self.store.manifest.list_entries(scope)
        except Exception:
            return True  # fail open: an unavailable manifest must not strip a home
        by_id = {e.wiki_id: e for e in entries}
        known: set[str] = set()
        for wid in matched[:5]:
            entry = by_id.get(wid)
            if entry is None:
                continue
            # ONLY the compact summary counts as topical vocabulary. The
            # entity's own title/aliases are deliberately EXCLUDED: those are
            # the names whose mere citation drives the score up, and including
            # them would make every name mention "topical" — collapsing the
            # very distinction this flag exists to draw (a reference vs. a
            # subject).
            if entry.compact:
                known |= _content_tokens(entry.compact)
        if not known:
            return False
        shared = incoming & known
        # Two distinct shared content words are needed before we call the
        # subject overlapping. One shared word is NOT enough: entity summaries
        # accumulate generic verbs and adjectives ("wants", "looking",
        # "open") that co-occur with unrelated text purely by chance — the
        # espresso case shares "wants" with Acme Corp's summary but is not
        # about Acme's work. Two domain words make a real topical connection.
        return len(shared) >= 2

    def _is_coherent(self, wiki: WikiMeta, matched: list[str]) -> bool:
        """True when the matched entities' own content agrees with the wiki's
        identity (title / description / topic) -- i.e. the entities belong to
        this wiki's topic.

        This replaces a naive text-vs-identity check, which failed on real
        continuations: a follow-up like "Maria ordered the replacement Keurig"
        never literally repeats the wiki's title word ("coffee"), so a
        text-overlap test called it incoherent even though the Keurig entity's
        summary ("the coffee machine that keeps breaking") clearly belongs to
        an Office Coffee wiki.

        Instead, compare the MATCHED ENTITIES' compact summaries against the
        wiki identity:
          - Contaminated case: Maria's summary ("researching coffee machine
            models") vs the Azure wiki identity ("Q3 Azure migration") share
            no content words -> incoherent. The misfit entity's topic disagrees
            with the wiki that absorbed it.
          - Genuine case: the Keurig entity summary ("office coffee machine")
            vs the Office Coffee identity share content words -> coherent.
        """
        from app.verify.assessor import _content_tokens
        identity = " ".join([
            wiki.title or "",
            wiki.description or "",
            getattr(wiki, "topic", "") or "",
        ])
        known = _content_tokens(identity)
        if not known:
            # No identity words to judge against (a bare "New Wiki" shell):
            # cannot say it is incoherent, so treat as coherent to avoid
            # over-eager new-topic routing.
            return True
        try:
            entries = self.store.manifest.list_entries(wiki.wiki_id)
        except Exception:
            return True
        by_id = {e.wiki_id: e for e in entries}
        for wid in (matched or [])[:5]:
            entry = by_id.get(wid)
            if entry is None or not entry.compact:
                continue
            ent_tokens = _content_tokens(entry.compact)
            if ent_tokens and (ent_tokens & known):
                # Found an entity whose content agrees with the wiki identity.
                return True
        # None of the matched entities' content overlaps the wiki identity:
        # they read as belonging to a different topic than the wiki claims.
        return False

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

        # TAG GATE — "skip and search other wikis." The top-scoring wiki may
        # have matched on entity NAMES while its curated TAGS disagree with the
        # text's subject (a tagged wiki is a strong, explicit wrong-topic
        # signal — e.g. a wiki tagged [search, migration] matched "espresso
        # machine" through a stray entity). When the best candidate is tag-
        # mismatched:
        #   * if SOME other viable wiki IS tag-aligned, that tagged wiki is the
        #     right home -> use it (search the others instead of absorbing into
        #     the wrong topic);
        #   * if NO viable candidate is tag-aligned, the text belongs to a
        #     different topic entirely -> not a `use`; route create/query below
        #     (by forcing `strong=False`, a tag-mismatched best can never win a
        #     write as a continuation).
        if best.matched and not best.tags_match:
            tag_aligned = next((c for c in viable if c.tags_match), None)
            if tag_aligned is not None and tag_aligned.wiki_id != best.wiki_id:
                return RouteDecision(
                    action="use", wiki_id=tag_aligned.wiki_id,
                    candidates=viable[:5],
                    reason=(f"{best.title} is tagged for a different topic; "
                            f"skipped it and used {tag_aligned.title} (tags match)."))
            # No tag-aligned home in reach: this text is a DIFFERENT topic, not
            # a continuation. Demote `best` so the cascade cannot turn it into
            # a `use`.
            best = WikiCandidate(
                wiki_id=best.wiki_id, title=best.title, score=best.score,
                matched=best.matched, strong=False, topical=best.topical,
                coherent=best.coherent, tags_match=False,
                reason=f"{best.title} is tagged for a different topic; "
                       f"the text does not match its tags.")

        # A text counts as a NEW TOPIC when neither the best nor any tied
        # candidate is a STRONG (real named-entity) match AND the text names
        # entities none of them actually knows. Computing it once here lets us
        # use it both to resolve thin TIES and to fire the thin-match escape.
        new_names = _proper_noun_phrases(text)
        known_names = {n.lower() for c in viable for n in _known_entity_names(c)}
        # MATCH BY CONTENT TOKENS, not exact phrase. _proper_noun_phrases
        # over-captures (“The Q3 Azure” as one phrase) while entity names are
        # narrower (“q3 azure migration”), so an exact-phrase comparison flags
        # a genuine continuation as “introduces new entities” and wrongly
        # routes it to a new wiki. A name is KNOWN if its meaningful words
        # overlap the known names; only a name sharing no known content words
        # counts as discovery.
        known_toks = {w for n in known_names for w in n.split() if len(w) > 1}

        def _is_discovery(phrase: str) -> bool:
            words = {w for w in phrase.lower().split() if len(w) > 1}
            if not words:
                return False
            real = words - _NAMEPHRASE_STOPWORDS
            return bool(real) and not (real & known_toks)

        has_discovery = any(_is_discovery(n) for n in new_names)
        is_new_topic = (not best.strong) and has_discovery

        if is_new_topic and allow_create:
            # Neither the best nor a tied candidate really matches the text
            # (all thin word-overlap) and it introduces new names -> a
            # genuinely new topic. Do NOT report a thin-tie as ambiguous nor
            # a thin-winner as "use"; propose a NEW independent wiki instead.
            title = self.propose_title(text)
            return RouteDecision(
                action="create", candidates=viable[:5],
                proposed_title=title,
                reason=(f"Best match {best.title!r} is only weak "
                        f"(score {best.score}) and the text introduces "
                        f"new named entities; looks like a new topic "
                        f"rather than a continuation. Proposed a new wiki."))

        if runner_up and (best.score - runner_up.score) < TIE_MARGIN and not best.matched:
            # A tie between candidates that matched the text on real content
            # (not just by sharing an entity name) is genuinely ambiguous - a
            # real membership conflict where the text could belong to either.
            # But a tie driven purely by a SHARED entity NAME (best.matched
            # non-empty, e.g. the same person appears in two unrelated wikis)
            # is NOT a membership conflict: the text merely references a
            # known entity while its own subject is unconfirmed. We must NOT
            # deadlock on that false tie -- falling through to the query
            # branch lets the LLM decide whether it is a new topic instead.
            return RouteDecision(
                action="ambiguous", candidates=viable[:5],
                reason=f"{best.title} and {runner_up.title} scored within "
                       f"{TIE_MARGIN} of each other.")

        # The caller needs a "use" to imply "this text belongs to this
        # wiki's TOPIC". A strong score driven purely by name mention
        # (topical=False) does not establish that: the text cites entities
        # the wiki knows but its subject may be a different topic ("Alice
        # Chen wants a new espresso machine" cites Alice but is not about
        # her work). Blindly writing such text into that wiki is exactly the
        # "unrelated nodes forcibly stored in a wiki page" failure. We do
        # NOT guess — the LLM's topic judgment (called by the chat and
        # extract paths on this action) decides whether it is a continuation
        # or a genuinely new topic. `best.wiki_id` is still reported so the
        # caller can fall back to it if the LLM is unavailable.
        #
        # Guard: only fire when there ARE entity matches (best.matched
        # non-empty). A wiki with NO entity matches (the empty-home bootstrap
        # case) never has a summary tier, so topical is False there too — but
        # that is a legitimate continuation, not a reference to a different
        # topic, and must keep routing home.
        if best.matched and not best.topical:
            return RouteDecision(
                action="query", wiki_id=best.wiki_id, candidates=viable[:5],
                reason=(f"{best.title} matched on names ({', '.join(best.matched[:3])}) "
                        f"but not on topic; ask whether this starts a new wiki."))

        # The self-reinforcing contamination trap: best matched real entities
        # topically, but those entities' summaries carry a topic that does NOT
        # align with the wiki's own identity (title/description/topic). That
        # happens when an unrelated entity was previously written into this
        # wiki and its summary baked in the *other* topic -- the wiki now
        # matches every future message about that topic forever. (Observed:
        # a coffee/office message routing to a "Q3 Azure Migration" wiki at
        # 0.99 because Maria/Facilities entities with coffee summaries lived
        # there.) A genuine continuation of the wiki's topic stays coherent
        # (its matched entities overlap the wiki identity), so this only
        # demotes misfits.
        if best.matched and not best.coherent:
            # If some OTHER candidate is coherent (its entities genuinely
            # belong to its own topic), prefer it over the contaminated
            # best -- e.g. Maria living in both a contaminated Azure wiki and
            # a proper Office-Coffee wiki must not lasso the coffee follow-up
            # into a new-wiki decision; the coherent home is right there.
            coherent_home = next((c for c in viable
                                  if (c.matched and c.coherent)), None)
            if coherent_home is not None and coherent_home.wiki_id != best.wiki_id:
                return RouteDecision(
                    action="use", wiki_id=coherent_home.wiki_id,
                    candidates=viable[:5], reason=coherent_home.reason)
            if allow_create:
                title = self.propose_title(text)
                return RouteDecision(
                    action="create", candidates=viable[:5],
                    proposed_title=title,
                    reason=(f"{best.title} matched entities ({', '.join(best.matched[:3])}) "
                            f"but their topic does not align with the wiki's own "
                            f"identity; looks like a contaminated or new topic rather "
                            f"than a continuation. Proposed a new wiki."))
            return RouteDecision(
                action="query", wiki_id=best.wiki_id, candidates=viable[:5],
                reason=(f"{best.title} matched entities ({', '.join(best.matched[:3])}) "
                        f"topically but incoherent with the wiki's own topic; "
                        f"ask whether this starts a new wiki."))

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
                             description: str = "",
                             topic: str = "") -> WikiMeta:
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
                                    description=description, wiki_id=slug,
                                    topic=topic)

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

    # ---------- topic system ----------

    def route_by_topic(self, user_id: str, text: str, llm,
                       role: str = ROLE_READ) -> tuple[RouteDecision, WikiMeta | None]:
        """Ask the LLM whether this message starts a new topic and, if so,
        create a wiki named after it; otherwise route to the best existing
        wiki.

        Returns (decision, wiki). The wiki is non-None only when the decision
        is "use" (either an existing wiki the LLM chose, or a newly created
        one). When the LLM says the topic is new and uncontroversial, the new
        wiki is created immediately so both chat and extraction can write into
        it; the caller surfaces `proposed_title`/`reason` for the UI toggle.

        Degrades gracefully: if the LLM is unavailable or disagrees with a
        new-topic call, it falls back to the deterministic router (which never
        auto-creates on its own for a write).
        """
        reachable = self.registry.list_for(user_id, role=role)
        existing = [{"wiki_id": w.wiki_id, "title": w.title, "topic": w.topic}
                    for w in reachable]
        verdict = llm.evaluate_new_topic(text, existing)

        if not verdict.get("is_new_topic"):
            belongs = verdict.get("belongs_to")
            if belongs:
                wiki = self.registry.get(belongs)
                if wiki is not None and self.registry._permits(wiki, user_id, role):
                    return (RouteDecision(action="use", wiki_id=wiki.wiki_id,
                                          reason=verdict.get("reason") or
                                                 f"belongs to {wiki.title}"), wiki)
            # LLM says continue but did not name a wiki: fall back to the
            # deterministic scorer.
            return self.route(user_id, text, role=role), None

        # New topic -> create a wiki named after it, tagged with its topic.
        title = (verdict.get("title") or "").strip() or verdict.get("topic") or "New Wiki"
        description = verdict.get("description") or ""
        topic = verdict.get("topic") or title
        try:
            wiki = self.create_from_decision(user_id, RouteDecision(action="create"),
                                             title=title, description=description,
                                             topic=topic)
        except WikiNameCollision as e:
            existing = self.registry.get(e.existing_id)
            return (RouteDecision(action="use", wiki_id=e.existing_id,
                                  reason=f"proposed {title!r} was too close to "
                                         f"{e.existing_title!r}; reused it"),
                    existing)
        decision = RouteDecision(action="use", wiki_id=wiki.wiki_id,
                                 proposed_title=wiki.title,
                                 reason=verdict.get("reason") or
                                        f"new topic: {topic}")
        return decision, wiki
