"""
Non-LLM assessment of a raw conversation: is it worth remembering, how much
does it matter, and what does it connect to?

This is PLAN.md §4 steps 1-2 — the part that runs before any model is
involved. §12's first Golden Rule is "never call an LLM just to check
usefulness", so everything here is deterministic string and set work over
data already in the manifest. No embeddings, no network, no model weights,
no extra dependencies.

WHAT IT PRODUCES
    An Assessment carrying four independent scores, the entities it matched
    in the existing graph, candidate new entities, and a decision. All of the
    intermediate signals are returned too, because a scoring function nobody
    can interrogate is a scoring function nobody will trust or tune.

THE FOUR SCORES (PLAN.md §2's metric families, made computable)

    relevance   How strongly this text attaches to memory that already
                exists — matches against entity titles, aliases and compact
                summaries. High relevance means "we know these things, this
                adds to them".

    novelty     How much of the informative content is NOT already recorded.
                Computed against the compact summaries of matched entities.
                A conversation that restates what is already stored scores
                low, and low novelty is the main reason to reject: §4 marks
                similarity > 0.8 to existing memory as redundant.

    importance  How much this seems to matter to the user, from linguistic
                markers: decisions, commitments, preferences, deadlines,
                corrections, negations of prior belief. This is the axis
                that separates "we shipped v2 today" from "how's the weather".

    density     Information per word — distinct entities and factual markers
                relative to length. §2 calls this semantic density. It is
                what stops a long rambling message outscoring a short
                consequential one purely by volume.

WHY RELEVANCE AND NOVELTY BOTH MATTER, IN OPPOSITE DIRECTIONS
    Relevance high + novelty low  -> redundant, we already know this. Skip.
    Relevance high + novelty high -> updates known entities. Store, high value.
    Relevance low  + novelty high -> a genuinely new topic. Store if it is
                                     also important or dense, otherwise it is
                                     indistinguishable from noise.
    Relevance low  + novelty low  -> chatter. Skip.

    Multiplying them into one number destroys this distinction, so they stay
    separate and the decision rule reads them as a pair.

WHAT IT DELIBERATELY DOES NOT DO
    It does not write anything. It reports what it would store and why,
    leaving the caller to act. That keeps it cheap to run on everything,
    which is the point of a first-stage filter.

    It does not extract structured facts. Deciding "this is worth keeping and
    it is about person/alice-chen" is a different, much cheaper problem than
    producing a well-formed fact sentence — that is the job §4 step 3 hands
    to the agent's own LLM response, at no extra call.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.graph.lexical import STOPWORDS, _WORD, _tokens, content_tokens, jaccard
from app.graph.manifest import ManifestEntry
from app.graph.store import EntityGraphStore
from app.graph.title_resolver import _normalize

# Back-compat aliases: several modules (autocapture/scheduler.py,
# routes_chat.py, routes_extract.py, extractor.py, wikis/router.py) do
# `from app.verify.assessor import _content_tokens` — the implementation now
# lives in app.graph.lexical, but the old private names stay importable here.
_content_tokens = content_tokens
_jaccard = jaccard

# Markers of things a person will want remembered. Grouped so the resulting
# signal explains itself in the output rather than collapsing to one number.
IMPORTANCE_MARKERS: dict[str, tuple[str, ...]] = {
    "decision": ("decided", "decide", "chose", "choosing", "agreed", "agreement",
                 "concluded", "settled on", "going with", "we'll use", "signed off",
                 "approved", "rejected", "vetoed", "committed to",
                 "决定", "决策", "确定", "敲定", "明确", "达成", "批准", "否决"),
    "commitment": ("will", "going to", "plan to", "deadline", "due", "by friday",
                   "by monday", "next week", "next month", "scheduled", "promised",
                   "owes", "action item", "todo", "follow up",
                   "计划", "排期", "于", "前完成", "截止", "预算", "目标", "时限",
                   "待办", "跟踪", "整改方案", "考核期"),
    "preference": ("prefer", "prefers", "preferred", "dislike", "dislikes",
                   "hate", "hates", "love", "loves", "always", "never",
                   "favourite", "favorite", "rather than", "instead of",
                   "can't stand", "wish", "偏好", "倾向", "更喜欢", "优于"),
    "correction": ("actually", "correction", "i was wrong", "not true", "mistake",
                   "no longer", "used to", "changed", "updated", "revised",
                   "turns out", "in fact", "纠正", "更正", "不是", "不再", "已调整",
                   "修订"),
    "identity": ("i am", "i'm", "my name", "works at", "works on", "role",
                 "title", "team", "reports to", "based in", "lives in", "joined",
                 "总监", "经理", "主管", "专员", "组长", "负责人", "主持",
                 "参会", "列席", "汇报"),
    "problem": ("issue", "bug", "broken", "failed", "failing", "error", "blocked",
                "blocker", "risk", "concern", "outage", "regression",
                "问题", "隐患", "短板", "风险", "违规", "不足", "缺口", "故障",
                "异常"),
}

# Not all markers carry equal weight. A stated preference or a correction is
# a durable fact about the user that stays true for months; a mention of a bug
# is usually transient. Weighting the GROUPS is what lets a short first-person
# statement outrank a long entity-free complaint.
MARKER_GROUP_WEIGHT: dict[str, float] = {
    "decision": 1.0,
    "preference": 1.0,
    "identity": 1.0,
    "correction": 0.9,
    "commitment": 0.8,
    "problem": 0.5,
}

# First and second person mark a statement as being ABOUT the user rather than
# about the world. "Importance to the user" is the thing being scored, and a
# self-referential sentence is the clearest available evidence of it without a
# model.
_FIRST_PERSON = re.compile(r"(?<![a-z])(i|i'm|i've|i'll|me|my|mine|we|our|ours)(?![a-z])")

# Sentences with these read as transient chatter regardless of length.
CHATTER_MARKERS = ("hello", "hi ", "hey ", "thanks", "thank you", "goodbye",
                   "bye", "good morning", "good night", "how are you", "lol",
                   "haha", "ok", "okay", "sure", "sounds good", "no problem")

# Runs of capitalised words: the cheapest usable proper-noun detector without
# a POS tagger. Over-fires on sentence starts, which _strip_sentence_initial
# handles. CJK has no capitalisation, so also capture runs of Han characters
# (Chinese/Japanese names, org/team names like "土建班组", "技术部") as
# name candidates -- without this the assessor sees NO named entities in a
# Chinese meeting and skips it as "no named entities".
_PROPER = re.compile(r"\b([A-Z][a-z0-9'’\-]+(?:\s+(?:of|de|van|der|the)?\s*[A-Z][a-z0-9'’\-]+)*)|([\u3400-\u9fff]{2,8})")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMERIC = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|ms|s|k|m|bn|usd|eur|gbp|twd)?\b", re.I)
_DATEISH = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\b|\b\d{4}-\d{2}-\d{2}\b|\bq[1-4]\b|\b20\d{2}\b", re.I)

# A relational identity fact stated as "X is a/an/the <role> at/in/for/with
# <Organization>". The identity IMPORTANCE_MARKERS only match literal verbs
# ("works at", "reports to", "team lead", ...) and so MISS the extremely
# common "is a ... at ..." phrasing ("Ravi Shah is a data engineer at
# Silverline Analytics"). Such a sentence is a durable, valuable fact about
# an entity; without this it trips the min_words gate as "chatter" and is
# discarded before any wiki page is ever created. Firing here is treated as a
# durable identity signal, bypassing the length gate the same way the literal
# markers do. The terminal org word is required to be capitalized so the
# pattern does not fire on chatter like "is a thing for me".
_IDENT_RELATIONAL = re.compile(
    r"\bis (?:a|an|the)?\s+[a-z][a-z .'’\-]{1,50}?\s+(?:at|in|for|with)\s+[A-Z]",
    re.I,
)


@dataclass
class LinkedEntity:
    """An existing graph entity this text appears to be about."""
    wiki_id: str
    title: str
    type: str
    confidence: float
    matched_on: str       # "title" | "alias" | "name-fragment" | "summary" | "semantic"
    mentions: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Assessment:
    decision: str                 # "store" | "review" | "skip"
    usefulness: float             # 0..1 headline score
    relevance: float
    novelty: float
    importance: float
    density: float
    related: list[LinkedEntity] = field(default_factory=list)
    new_candidates: list[str] = field(default_factory=list)
    key_sentences: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["related"] = [r.to_dict() for r in self.related]
        return d


_MARKER_CACHE: dict[str, re.Pattern] = {}


def _marker_present(marker: str, lowered: str) -> bool:
    pat = _MARKER_CACHE.get(marker)
    if pat is None:
        pat = re.compile(rf"(?<![a-z]){re.escape(marker)}(?![a-z])")
        _MARKER_CACHE[marker] = pat
    return bool(pat.search(lowered))


def _strip_sentence_initial(text: str, phrases: list[str]) -> list[str]:
    """Drop proper-noun candidates that are only capitalised because they
    start a sentence. Without this, "The deadline moved" yields "The" and
    every message looks entity-rich."""
    starts = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if sentence:
            first = _WORD.search(sentence)
            if first:
                starts.add(first.group(0))
    out = []
    for p in phrases:
        words = p.split()
        if len(words) == 1 and words[0] in starts:
            continue
        out.append(p)
    return out


def _proper_noun_phrases(text: str) -> list[str]:
    raw = [(m.group(1) or m.group(2)).strip() for m in _PROPER.finditer(text)]
    raw = [p for p in raw if p and p.lower() not in STOPWORDS]
    return _strip_sentence_initial(text, raw)


class ConversationAssessor:
    """Scores raw conversation text against a user's existing graph.

    Read-only: it takes an EntityGraphStore purely to consult the manifest,
    which already holds every title, alias and compact summary. That means a
    full assessment costs ONE manifest read (usually served from the
    in-memory cache) and no entity-file reads at all — which is what keeps it
    inside PLAN.md §4's 1-5ms budget for the rule-based stage.
    """

    def __init__(
        self,
        store: EntityGraphStore,
        min_words: int = 12,
        store_threshold: float = 0.55,
        review_threshold: float = 0.32,
        redundancy_threshold: float = 0.80,
    ):
        self.store = store
        self.min_words = min_words
        self.store_threshold = store_threshold
        self.review_threshold = review_threshold
        # §4 treats similarity > 0.8 against existing memory as redundant.
        self.redundancy_threshold = redundancy_threshold

    # ---------- stage 1: cheap structural signals ----------

    def _importance(self, lowered: str) -> tuple[float, dict]:
        hits: dict[str, list[str]] = {}
        for group, markers in IMPORTANCE_MARKERS.items():
            # Word-boundary, not substring: plain `"like" in text` fires on
            # "unlikely", "like that" and "likely", which turned ordinary
            # chatter into a preference statement.
            found = [m for m in markers if _marker_present(m, lowered)]
            if found:
                hits[group] = found
        # Breadth of marker GROUPS matters more than raw count: a message
        # that both decides something and sets a deadline is more consequential
        # than one that says "prefer" five times. Groups are weighted, so
        # breadth is measured in significance rather than in count.
        max_weight = sum(sorted(MARKER_GROUP_WEIGHT.values(), reverse=True)[:3])
        weighted = sum(MARKER_GROUP_WEIGHT.get(g, 0.5) for g in hits)
        breadth = min(1.0, weighted / max_weight) if max_weight else 0.0
        depth = min(1.0, sum(len(v) for v in hits.values()) / 8.0)

        # Self-reference: is this about the user, or about the world?
        self_ref = min(1.0, len(_FIRST_PERSON.findall(lowered)) / 3.0)

        score = min(1.0, 0.55 * breadth + 0.2 * depth + 0.25 * self_ref)
        signals = {k: v[:4] for k, v in hits.items()}
        if self_ref:
            signals["_self_referential"] = round(self_ref, 2)
        return score, signals

    def _density(self, text: str, words: list[str], propers: list[str]) -> tuple[float, dict]:
        n = max(len(words), 1)
        numerics = len(_NUMERIC.findall(text))
        dates = len(_DATEISH.findall(text))
        distinct_propers = len({p.lower() for p in propers})
        # Facts tend to arrive as named things plus concrete values.
        raw = (distinct_propers * 2.0 + numerics + dates) / n
        return min(1.0, raw * 6.0), {
            "words": n,
            "distinct_proper_nouns": distinct_propers,
            "numeric_mentions": numerics,
            "date_mentions": dates,
        }

    # ---------- stage 2: linkage against existing memory ----------

    def _link(self, entries: list[ManifestEntry], text: str,
              propers: list[str], user_id: str) -> list[LinkedEntity]:
        lowered = text.lower()
        norm_propers = {_normalize(p): p for p in propers}
        found: dict[str, LinkedEntity] = {}

        for e in entries:
            if e.status != "active":
                continue

            norm_title = _normalize(e.title)
            confidence = 0.0
            matched_on = ""
            mentions = 0

            # Case-insensitive fallbacks below: people type "who is alice?",
            # not "Who is Alice?". Matching only capitalised mentions made
            # retrieval fail on ordinary lowercase questions.
            if norm_title and norm_title in norm_propers:
                confidence, matched_on = 0.95, "title"
                mentions = lowered.count(e.title.lower())
            elif e.title.lower() in lowered:
                confidence, matched_on = 0.85, "title"
                mentions = lowered.count(e.title.lower())
            else:
                for alias in e.aliases:
                    na = _normalize(alias)
                    if not na or len(na) < 3:
                        continue
                    if na in norm_propers or re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
                        confidence, matched_on = 0.7, "alias"
                        mentions = len(re.findall(rf"\b{re.escape(alias.lower())}\b", lowered))
                        break

            # Name-fragment / first-name tier: "whos Anton" should find
            # "Anton Lokhy", "who is Alice" should find "Alice Chen". The full
            # title tiers above fail on these because the question carries only
            # part of the name. We match when a proper-noun mention is a leading
            # token-prefix of the entity title (a first-name lookup). All
            # mention tokens must appear in the title and the mention must start
            # the title, which stays conservative: "John" finds "John Smith"
            # but never "Anna Johnson" or a generic noun.
            if confidence == 0.0 and norm_title:
                title_tokens = norm_title.split()
                for pm in norm_propers:
                    mt = pm.split()
                    if (mt and len(title_tokens) >= len(mt)
                            and title_tokens[:len(mt)] == mt
                            and all(len(t) >= 3 for t in mt)
                            and any(len(t) >= 3 for t in title_tokens)):
                        confidence, matched_on = 0.6, "name-fragment"
                        mentions = 1
                        break

            if confidence == 0.0 and e.compact:
                # Weakest tier: topical overlap with what we already summarised.
                overlap = jaccard(content_tokens(e.compact), content_tokens(text))
                if overlap >= 0.18:
                    confidence, matched_on, mentions = 0.35 + overlap / 2, "summary", 1

            if confidence > 0:
                found[e.wiki_id] = LinkedEntity(
                    wiki_id=e.wiki_id, title=e.title, type=e.type,
                    confidence=round(min(confidence, 1.0), 3),
                    matched_on=matched_on, mentions=max(mentions, 1),
                )

        if not found:
            # Last resort, tried only on a total miss above (see
            # app/graph/embeddings.py for why this stays rare rather than
            # running on every message): a query and an entity description
            # can be genuinely related with zero shared text -- "department"
            # and "branch" share no characters but are the same kind of
            # thing. A no-op unless EMBEDDING_ENABLED is set and the model
            # is actually available.
            found.update(self._semantic_link(entries, text, user_id))

        return sorted(found.values(), key=lambda x: (-x.confidence, x.wiki_id))

    def _semantic_link(self, entries: list[ManifestEntry], text: str,
                       user_id: str) -> dict[str, LinkedEntity]:
        from app.graph.embeddings import cosine, embed, is_embedding_enabled

        if not is_embedding_enabled():
            return {}
        text_vector = embed(text)
        if text_vector is None:
            return {}
        vectors = self.store.embedding_index.get_all(user_id)
        if not vectors:
            return {}

        from app.config import get_settings
        floor = get_settings().embedding_similarity_floor

        by_id = {e.wiki_id: e for e in entries if e.status == "active"}
        out: dict[str, LinkedEntity] = {}
        for wiki_id, vector in vectors.items():
            entry = by_id.get(wiki_id)
            if entry is None:
                continue
            similarity = cosine(text_vector, vector)
            if similarity >= floor:
                out[wiki_id] = LinkedEntity(
                    wiki_id=entry.wiki_id, title=entry.title, type=entry.type,
                    confidence=round(min(similarity, 1.0), 3),
                    matched_on="semantic", mentions=1,
                )
        return out

    def _novelty(self, text: str, related: list[LinkedEntity],
                 entries_by_id: dict[str, ManifestEntry]) -> tuple[float, dict]:
        """How much of this is NOT already in the compact summaries of the
        entities it refers to. This is the redundancy check from §4, done with
        token sets rather than embeddings."""
        incoming = content_tokens(text)
        if not incoming:
            return 0.0, {"reason": "no content tokens"}

        known: set[str] = set()
        for r in related:
            entry = entries_by_id.get(r.wiki_id)
            if not entry:
                continue
            if entry.compact:
                known |= content_tokens(entry.compact)
            # The entity's own title and aliases count as known. Without this,
            # "Alice Chen is a staff engineer in Taipei" scored as novel
            # against the summary "Staff engineer on the retrieval team in
            # Taipei" purely because the summary does not repeat the name.
            known |= content_tokens(entry.title)
            for alias in entry.aliases:
                known |= content_tokens(alias)

        if not known:
            # Nothing to be redundant against.
            return 1.0, {"known_tokens": 0, "reason": "no prior summary to compare"}

        covered = len(incoming & known) / len(incoming)
        return round(1.0 - covered, 3), {
            "known_tokens": len(known),
            "incoming_tokens": len(incoming),
            "already_covered": round(covered, 3),
        }

    def _key_sentences(self, text: str, related: list[LinkedEntity]) -> list[str]:
        """The sentences a summariser should look at first: those carrying
        importance markers or naming a linked entity."""
        names = {r.title.lower() for r in related}
        scored = []
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            if len(sentence.split()) < 4:
                continue
            low = sentence.lower()
            score = sum(1 for markers in IMPORTANCE_MARKERS.values()
                        if any(m in low for m in markers))
            score += 2 * sum(1 for n in names if n and n in low)
            score += 0.5 * len(_NUMERIC.findall(sentence))
            if score > 0:
                scored.append((score, sentence))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:5]]

    # ---------- public ----------

    def assess(self, user_id: str, text: str, *, link_only: bool = False) -> Assessment:
        """Score text and link it to existing entities.

        `link_only` skips the structural rejects below. They exist to decide
        what is worth STORING — text with no proper nouns and no importance
        markers is not worth keeping — and they return before _link() runs, so
        `related` comes back empty.

        That is wrong for RETRIEVAL. "who is alice?" is lowercase, so it has no
        proper nouns and no importance markers, and the early exit meant the
        chatbot found nothing for exactly the questions people actually type.
        Whether text is worth keeping and what it refers to are different
        questions; only the second matters when answering.
        """
        reasons: list[str] = []
        words = _tokens(text)
        lowered = text.lower()

        # --- hard structural rejects, before any graph work (§4 step 1) ---
        # A strong importance marker overrides the length gate. "I prefer
        # async written updates over status meetings" is eleven words and is
        # precisely the kind of durable preference this system exists to
        # keep; a flat minimum would discard it.
        # Some marker groups signal durable facts about the user regardless of
        # how briefly they are expressed. A numeric importance threshold does
        # not work here: "I prefer async updates over meetings, always have"
        # fires only one group and scores 0.19, below any threshold that also
        # excludes chatter. Name the groups instead.
        _, early_markers = self._importance(lowered)
        durable = ({"decision", "preference", "correction", "identity"}
                   & set(early_markers))
        # "is a <role> at/in/for/with <Org>" is a durable identity fact even
        # though it uses none of the literal identity verbs.
        if _IDENT_RELATIONAL.search(lowered):
            durable.add("identity")
        if not link_only and len(words) < self.min_words and not durable:
            return Assessment(
                decision="skip", usefulness=0.0, relevance=0.0, novelty=0.0,
                importance=0.0, density=0.0,
                signals={"words": len(words), "importance_markers": early_markers},
                reasons=[f"too short ({len(words)} words, minimum {self.min_words}) "
                         f"with no durable-fact markers"],
            )

        propers = _proper_noun_phrases(text)
        importance, importance_signals = self._importance(lowered)
        density, density_signals = self._density(text, words, propers)

        stripped = lowered.strip()
        if not link_only and not propers and importance == 0.0:
            return Assessment(
                decision="skip", usefulness=0.0, relevance=0.0, novelty=0.0,
                importance=0.0, density=density,
                signals={**density_signals, "proper_nouns": []},
                reasons=["no named entities and no importance markers"],
            )
        if (not link_only and any(stripped.startswith(c) for c in CHATTER_MARKERS)
                and importance < 0.2 and not propers):
            return Assessment(
                decision="skip", usefulness=0.0, relevance=0.0, novelty=0.0,
                importance=importance, density=density,
                signals=density_signals, reasons=["conversational filler"],
            )

        # --- graph linkage (§4 step 2) ---
        entries = self.store.manifest.list_entries(user_id)
        entries_by_id = {e.wiki_id: e for e in entries}
        related = self._link(entries, text, propers, user_id)

        if related:
            top = sum(r.confidence for r in related[:3]) / min(len(related), 3)
            relevance = round(min(1.0, top * (1 + 0.1 * (len(related) - 1))), 3)
        else:
            relevance = 0.0

        novelty, novelty_signals = self._novelty(text, related, entries_by_id)

        linked_titles = {_normalize(r.title) for r in related}
        linked_aliases = {_normalize(a) for r in related
                          for a in (entries_by_id[r.wiki_id].aliases if r.wiki_id in entries_by_id else [])}
        new_candidates = [
            p for p in dict.fromkeys(propers)
            if _normalize(p) not in linked_titles and _normalize(p) not in linked_aliases
        ]

        # --- combine ---
        # Importance and density carry the most weight because they are the
        # only signals available when the graph is empty; relevance is
        # necessarily 0 for the first conversation a user ever has, and a
        # scorer that requires prior memory can never bootstrap.
        #
        # `discovery` exists for the same reason: text introducing several
        # unknown named entities is exactly what a memory system should
        # capture, but it scores 0 relevance by definition, so without a
        # dedicated term the very first mention of every person and project
        # lands in "review" and never gets stored.
        discovery = min(1.0, len(new_candidates) / 3.0)
        usefulness = round(
            0.30 * importance + 0.22 * density + 0.18 * novelty
            + 0.15 * relevance + 0.15 * discovery, 3
        )

        durable_groups = {"decision", "preference", "correction", "identity"} & set(importance_signals)
        self_referential = "_self_referential" in importance_signals
        durable_self_fact = bool(durable_groups) and self_referential and novelty >= 0.5

        redundant = bool(related) and (1.0 - novelty) >= self.redundancy_threshold
        if redundant:
            decision = "skip"
            reasons.append(
                f"redundant: {round((1 - novelty) * 100)}% already covered by "
                f"{', '.join(r.wiki_id for r in related[:3])}"
            )
        elif usefulness >= self.store_threshold:
            decision = "store"
        elif usefulness >= self.review_threshold:
            decision = "review"
            reasons.append("borderline — worth a second-stage check")
        elif durable_self_fact:
            # Short first-person statements of preference, identity, decision
            # or correction carry no entities and no numbers, so they cannot
            # reach the threshold on score alone — but they are durable facts
            # about the user and are exactly what should be remembered.
            decision = "store"
            reasons.append(
                "durable self-referential fact ("
                + ", ".join(sorted(durable_groups)) + ")")
        else:
            decision = "skip"
            reasons.append(f"usefulness {usefulness} below {self.review_threshold}")

        if importance_signals:
            reasons.append("importance markers: " + ", ".join(sorted(importance_signals)))
        if related:
            reasons.append("links to " + ", ".join(r.wiki_id for r in related[:5]))
        if new_candidates:
            reasons.append(f"{len(new_candidates)} unrecognised named entities")

        return Assessment(
            decision=decision,
            usefulness=usefulness,
            relevance=relevance,
            novelty=novelty,
            importance=round(importance, 3),
            density=round(density, 3),
            related=related,
            new_candidates=new_candidates[:20],
            key_sentences=self._key_sentences(text, related),
            signals={
                **density_signals,
                "proper_nouns": list(dict.fromkeys(propers))[:20],
                "importance_markers": importance_signals,
                "novelty": novelty_signals,
                "entities_in_graph": len(entries),
            },
            reasons=reasons,
        )
