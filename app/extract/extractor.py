"""
Turn raw conversation into graph operations, using an LLM.

WHERE THIS SITS
    assessor (no LLM)  ->  extractor (LLM)  ->  graph writes
    "is this worth       "what exactly       "apply it"
     remembering?"        is in it?"

    The assessor runs first and its verdict gates the LLM call. PLAN.md §12's
    first Golden Rule is not to spend a model call deciding whether something
    matters when cheap signals already answer that — chatter costs nothing
    here because it never reaches this module.

PLAN, THEN APPLY
    Extraction returns a PLAN: a list of proposed operations with the reason
    for each. Nothing is written until the plan is applied, and applying is a
    separate call.

    This is the important design decision. A model that writes directly to
    long-term memory can corrupt it in ways that are hard to notice and
    harder to undo — a wrong fact attached to the right entity looks exactly
    like a right one. A plan can be read, edited, partially accepted, or
    thrown away. It also makes the layer testable without a live model, and
    auditable afterwards.

RESOLUTION AGAINST WHAT ALREADY EXISTS
    Every extracted entity is matched against the graph before anything is
    written, so "Alice" attaches to an existing `person/alice-chen` instead
    of creating a second Alice. Without this the layer would degrade memory
    on every run — the failure mode is silent duplication, which is worse
    than an error because nothing surfaces it.

WHAT THE MODEL IS AND IS NOT TRUSTED WITH
    Trusted: reading prose and proposing structure.
    Not trusted: entity identity, schema validity, or what finally gets
    written. Types are checked against VALID_TYPES, relation categories
    against VALID_RELATION_CATEGORIES, targets must exist or be created in
    the same plan, and confidence is clamped. Anything that fails validation
    is dropped from the plan with a recorded reason rather than passed
    through.
"""

from __future__ import annotations

import re

import json
import logging
from dataclasses import asdict, dataclass, field

from app.extract.llm import (LLMClient, LLMError, LLMTruncated, extract_json,
                             salvage_truncated_json)
from app.graph.store import (VALID_RELATION_CATEGORIES, VALID_TYPES,
                             EntityGraphStore, SlugConflictError, has_usable_slug)
from app.graph.title_resolver import WikiTitleResolver, _normalize
from app.verify.assessor import ConversationAssessor

logger = logging.getLogger("memory_backend.extract")


# Words that carry no distinguishing information for this purpose. Kept small
# and deliberately excluding negations: "does not use BM25" and "uses BM25"
# must never collapse into one fact.
_FILLER = frozenset("""
a an the this that these those is are was were be been being am
of to in on at by for from with and or as it its their his her our your my
he she they we you i him them us me
""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-/.]*")
# Anything containing a digit: dates, versions, counts, percentages, money.
# These are the payload of most facts and the thing a correction changes.
_HAS_DIGIT = re.compile(r"\d")


def _fact_key(text: str) -> str:
    """Exact-match comparison form: case and surrounding punctuation only."""
    return " ".join(text.lower().split()).strip(".,;:!?'\"")


def _fact_signature(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """(content words, numeric tokens) used to judge near-duplicates.

    Numbers are separated out because they must be compared exactly while the
    prose around them is compared loosely. "The deadline is 2026-08-15" and
    "Deadline is 2026-09-01" share almost every word and are NOT the same
    fact — one supersedes the other, and silently merging them destroys the
    correction this layer exists to capture.
    """
    tokens = _TOKEN.findall(text.lower())
    numbers = frozenset(t.strip(".,") for t in tokens if _HAS_DIGIT.search(t))
    words = frozenset(t for t in tokens
                      if t not in _FILLER and not _HAS_DIGIT.search(t) and len(t) > 1)
    return words, numbers


def _is_near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when two facts say the same thing in different words.

    Exact-text matching was not enough in practice: the model phrases the same
    claim differently across runs — "Leads the retrieval workstream" one time,
    "Alice leads the retrieval workstream" the next — so re-extracting an
    overlapping conversation accumulated near-identical facts.

    Any difference in NUMBERS blocks the merge outright, whatever the prose
    similarity. That keeps corrections and version bumps as separate facts,
    which is the behaviour worth protecting: a duplicate is untidy, a lost
    correction is wrong.
    """
    a_words, a_nums = _fact_signature(a)
    b_words, b_nums = _fact_signature(b)
    if a_nums != b_nums:
        return False
    if not a_words or not b_words:
        return a_words == b_words

    shared = len(a_words & b_words)
    # Containment, not Jaccard. The commonest rephrasing is adding or dropping
    # the subject — "Leads the retrieval workstream" vs "Alice leads the
    # retrieval workstream" — because facts are stored against an entity whose
    # name is often implied. Jaccard scores that 0.75 and treats them as
    # different; containment scores 1.0, which is the truth: one says
    # everything the other says.
    containment = shared / min(len(a_words), len(b_words))
    if containment < threshold:
        return False
    # Guard against a short fact being swallowed by a long unrelated one that
    # happens to contain its words. "Leads." is contained in almost anything.
    return max(len(a_words), len(b_words)) - shared <= 2

MAX_CONVERSATION_CHARS = 24000

SYSTEM_PROMPT = """\
You extract durable, long-term memory from conversations.

Return ONLY a JSON object with this exact shape:

{
  "entities": [
    {
      "title": "Alice Chen",
      "type": "person",
      "aliases": ["Alice"],
      "summary": "One or two sentences of durable description.",
      "facts": [
        {"text": "Leads the retrieval workstream on Orion.", "confidence": 0.9}
      ]
    }
  ],
  "relations": [
    {
      "source": "Alice Chen",
      "target": "Orion",
      "category": "related_to",
      "label": "leads",
      "reason": "She was described as leading it."
    }
  ],
  "discarded": ["Anything you deliberately left out, and why."]
}

Valid types: person, organization, project, event, concept, artifact, preference, decision
Valid categories: related_to, refines, contradicts, causes, temporal_before, temporal_after

Rules:
- Record only what will still be true and useful weeks from now. Skip
  pleasantries, transient state, and anything said in passing.
- A fact is one atomic claim. Split compound statements.
- confidence: 1.0 stated as fact, 0.7-0.9 implied, below 0.5 do not record.
- Prefer attaching facts to entities that were mentioned by name.
- "source" and "target" in relations must exactly match a title in
  "entities", or the name of an entity listed as already known.
- Write summaries and facts as standalone statements. They will be read
  without the conversation for context, so "he said yes" is useless.
- Use the person's own words for preferences and decisions where possible.
- If nothing is worth remembering, return empty lists. That is a valid and
  useful answer.
"""


@dataclass
class Operation:
    op: str                     # "upsert_entity" | "add_fact" | "link_entities"
    wiki_id: str
    payload: dict
    reason: str = ""
    status: str = "proposed"    # "proposed" | "applied" | "failed" | "rejected"
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractionPlan:
    decision: str                       # assessor verdict that gated this
    operations: list[Operation] = field(default_factory=list)
    matched_entities: list[str] = field(default_factory=list)
    new_entities: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    assessment: dict = field(default_factory=dict)
    llm_used: bool = False
    applied: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["operations"] = [o.to_dict() for o in self.operations]
        return d


class ConversationExtractor:
    max_tokens: int = 8000

    def __init__(self, store: EntityGraphStore, llm: LLMClient,
                 min_decision: str = "review"):
        self.store = store
        self.llm = llm
        self.assessor = ConversationAssessor(store)
        self.resolver = WikiTitleResolver(store)
        # Which assessor verdicts are worth spending a model call on.
        # "review" includes borderline text; "store" is stricter and cheaper.
        self.min_decision = min_decision

    # ---------- planning ----------

    def plan(self, user_id: str, text: str, force: bool = False,
             evidence: str | None = None) -> ExtractionPlan:
        """Assess, then extract if worthwhile. Writes nothing."""
        assessment = self.assessor.assess(user_id, text)
        allowed = {"store"} if self.min_decision == "store" else {"store", "review"}

        if not force and assessment.decision not in allowed:
            # The cheap filter already answered. No model call.
            return ExtractionPlan(
                decision=assessment.decision,
                assessment=assessment.to_dict(),
                discarded=[f"Assessor returned {assessment.decision!r}: "
                           + "; ".join(assessment.reasons)],
                llm_used=False,
            )

        if len(text) > MAX_CONVERSATION_CHARS:
            # Truncate rather than refuse: the opening of a conversation is
            # usually where the durable content is, and a hard error would
            # lose all of it.
            logger.info("truncating conversation from %d to %d chars",
                        len(text), MAX_CONVERSATION_CHARS)
            text = text[:MAX_CONVERSATION_CHARS]

        known = self._known_entities(user_id, assessment)
        truncated_note: str | None = None
        try:
            raw = self.llm.complete(SYSTEM_PROMPT, self._user_prompt(text, known),
                                    max_tokens=self.max_tokens)
            data = extract_json(raw)
        except LLMTruncated as e:
            # The reply is incomplete, not malformed. It normally contains
            # several finished entities before the cut, and discarding them
            # would waste a call the user already paid for and force a retry
            # that may truncate again.
            data = salvage_truncated_json(e.text)
            if not data:
                raise
            recovered = len(data.get("entities", [])) + len(data.get("relations", []))
            truncated_note = (
                f"The model ran out of room (max_tokens={e.max_tokens}). Recovered "
                f"{recovered} complete item(s) from the partial reply; anything "
                f"after the cut is missing. Raise LLM_MAX_TOKENS or extract a "
                f"shorter passage to get the rest.")
            logger.warning("extraction truncated; salvaged %d item(s)", recovered)

        plan = self._build_plan(user_id, data, assessment, evidence=evidence)
        plan.llm_used = True
        if truncated_note:
            plan.rejected.append({"reason": truncated_note, "kind": "truncation"})
        return plan

    def _known_entities(self, user_id: str, assessment) -> list[str]:
        """Entities the assessor already linked, given to the model so it uses
        existing names instead of inventing near-duplicates."""
        entries = {e.wiki_id: e for e in self.store.manifest.list_entries(user_id)}
        out = []
        for r in assessment.related[:20]:
            entry = entries.get(r.wiki_id)
            if entry:
                out.append(f"- {entry.title} ({entry.type}): {entry.compact or 'no summary'}")
        return out

    def _user_prompt(self, text: str, known: list[str]) -> str:
        parts = []
        if known:
            parts.append("Entities already in memory that this conversation "
                         "appears to touch. Reuse these exact titles rather than "
                         "creating variants:\n" + "\n".join(known))
        parts.append("Conversation:\n\n" + text)
        return "\n\n".join(parts)

    # ---------- validation ----------

    def _build_plan(self, user_id: str, data: dict, assessment,
                    evidence: str | None = None) -> ExtractionPlan:
        plan = ExtractionPlan(decision=assessment.decision,
                              assessment=assessment.to_dict())
        plan.discarded = [str(d) for d in (data.get("discarded") or [])][:20]

        # Report anything the model returned that this parser does not read.
        # The schema nests facts INSIDE entities; a model that instead emits a
        # top-level "facts" array is not obviously wrong, and JSON mode will
        # not stop it. Without this the content vanishes with no trace in the
        # plan, which is the worst possible failure for a memory system —
        # silent loss that looks like success.
        known_keys = {"entities", "relations", "discarded"}
        for key in sorted(set(data) - known_keys):
            value = data[key]
            size = len(value) if isinstance(value, (list, dict)) else 1
            plan.rejected.append({
                "reason": f"Model returned an unrecognised top-level key {key!r} "
                          f"holding {size} item(s); the schema nests facts inside "
                          f"entities. Nothing from it was stored.",
                "raw": value if size <= 5 else f"{size} items",
            })

        entries = {e.wiki_id: e for e in self.store.manifest.list_entries(user_id)}
        by_title = {_normalize(e.title): e for e in entries.values()}
        for e in entries.values():
            for alias in e.aliases:
                by_title.setdefault(_normalize(alias), e)

        # title (as the model wrote it) -> wiki_id it will end up at
        resolved: dict[str, str] = {}
        # Facts already recorded on entities this plan touches. Extraction is
        # expected to run repeatedly over overlapping conversations, and
        # add_fact appends unconditionally, so without this the same claim
        # accumulates a copy per run and the entity fills with restatements.
        # Stored as the original text rather than a normalised key: exact
        # matching alone let rephrasings through, and near-duplicate detection
        # needs the real wording to compare.
        existing_facts: dict[str, list[str]] = {}

        for raw_entity in (data.get("entities") or [])[:40]:
            if not isinstance(raw_entity, dict):
                continue
            title = str(raw_entity.get("title") or "").strip()
            type_ = str(raw_entity.get("type") or "").strip().lower()

            if not title or not has_usable_slug(title):
                plan.rejected.append({"kind": "entity", "value": title,
                                      "why": "empty or unusable title"})
                continue
            if type_ not in VALID_TYPES:
                plan.rejected.append({"kind": "entity", "value": title,
                                      "why": f"invalid type {type_!r}"})
                continue

            existing = by_title.get(_normalize(title))
            if existing is not None and existing.type == type_:
                wiki_id = existing.wiki_id
                plan.matched_entities.append(wiki_id)
                if wiki_id not in existing_facts:
                    current = self.store.get_entity(user_id, wiki_id, touch=False)
                    existing_facts[wiki_id] = (
                        [f.text for f in current.facts] if current else [])
            else:
                wiki_id = self.store.compute_wiki_id(type_, title)
                plan.new_entities.append(wiki_id)
            resolved[_normalize(title)] = wiki_id

            aliases = [str(a).strip() for a in (raw_entity.get("aliases") or [])
                       if str(a).strip()][:10]
            summary = str(raw_entity.get("summary") or "").strip()
            plan.operations.append(Operation(
                op="upsert_entity", wiki_id=wiki_id,
                payload={"type": type_, "title": title, "aliases": aliases,
                         "summary_append": summary or None},
                reason=("update existing entity" if existing is not None
                        else "create new entity"),
            ))

            for raw_fact in (raw_entity.get("facts") or [])[:20]:
                if not isinstance(raw_fact, dict):
                    continue
                fact_text = str(raw_fact.get("text") or "").strip()
                if not fact_text:
                    continue
                try:
                    conf = float(raw_fact.get("confidence", 1.0))
                except (TypeError, ValueError):
                    conf = 1.0
                conf = max(0.0, min(1.0, conf))

                seen = existing_facts.setdefault(wiki_id, [])
                key = _fact_key(fact_text)
                duplicate_of = None
                for prior in seen:
                    if _fact_key(prior) == key or _is_near_duplicate(prior, fact_text):
                        duplicate_of = prior
                        break
                if duplicate_of is not None:
                    plan.rejected.append({
                        "kind": "fact", "value": fact_text[:80],
                        "why": f"already recorded on {wiki_id} as "
                               f"{duplicate_of[:60]!r}"})
                    continue
                seen.append(fact_text)

                payload = {"text": fact_text, "confidence": conf}
                if evidence:
                    # Records WHERE the claim came from, so "why do you believe
                    # this?" is answerable: find the fact through the graph at
                    # no storage cost, then one targeted GET for the exact
                    # conversation. Without it the graph is unauditable.
                    payload["evidence"] = [evidence]
                plan.operations.append(Operation(
                    op="add_fact", wiki_id=wiki_id,
                    payload=payload,
                    reason=f"stated about {title}",
                ))

        for raw_rel in (data.get("relations") or [])[:60]:
            if not isinstance(raw_rel, dict):
                continue
            src = _normalize(str(raw_rel.get("source") or ""))
            tgt = _normalize(str(raw_rel.get("target") or ""))
            category = str(raw_rel.get("category") or "related_to").strip().lower()

            src_id = resolved.get(src) or (by_title[src].wiki_id if src in by_title else None)
            tgt_id = resolved.get(tgt) or (by_title[tgt].wiki_id if tgt in by_title else None)

            if src_id is None or tgt_id is None:
                # The model referenced something it never defined. Dropping is
                # the only safe option: guessing the target is how a graph
                # silently acquires wrong edges.
                plan.rejected.append({
                    "kind": "relation",
                    "value": f"{raw_rel.get('source')} -> {raw_rel.get('target')}",
                    "why": "endpoint not among extracted or known entities"})
                continue
            if category not in VALID_RELATION_CATEGORIES:
                plan.rejected.append({"kind": "relation", "value": f"{src_id} -> {tgt_id}",
                                      "why": f"invalid category {category!r}"})
                continue
            if src_id == tgt_id:
                plan.rejected.append({"kind": "relation", "value": src_id,
                                      "why": "self-referential"})
                continue

            plan.operations.append(Operation(
                op="link_entities", wiki_id=src_id,
                payload={"target_wiki_id": tgt_id, "category": category,
                         "label": str(raw_rel.get("label") or "").strip()},
                reason=str(raw_rel.get("reason") or "").strip(),
            ))

        return plan

    # ---------- applying ----------

    def apply(self, user_id: str, plan: ExtractionPlan) -> ExtractionPlan:
        """Execute a plan. Each operation is independent — one failure does not
        abandon the rest, and every outcome is recorded on the operation."""
        for op in plan.operations:
            if op.status == "rejected":
                continue
            try:
                if op.op == "upsert_entity":
                    p = op.payload
                    self.store.upsert_entity(
                        user_id, p["type"], p["title"],
                        aliases=p.get("aliases") or None,
                        summary_append=p.get("summary_append"),
                        # Variant spellings should merge into what exists
                        # rather than 409 — the model producing "ALICE chen"
                        # for a known "Alice Chen" is expected, not an error.
                        on_conflict="disambiguate")
                elif op.op == "add_fact":
                    # evidence must be forwarded, not just recorded in the plan:
                    # it is what makes a stored claim traceable back to the
                    # conversation that produced it.
                    self.store.add_fact(user_id, op.wiki_id, op.payload["text"],
                                        confidence=op.payload.get("confidence", 1.0),
                                        evidence=op.payload.get("evidence") or [])
                elif op.op == "link_entities":
                    p = op.payload
                    self.store.link_entities(
                        user_id, op.wiki_id, p["target_wiki_id"],
                        category=p.get("category", "related_to"),
                        label=p.get("label", ""), reason=op.reason)
                else:
                    op.status, op.detail = "failed", f"unknown operation {op.op!r}"
                    continue
                op.status = "applied"
            except SlugConflictError as e:
                op.status, op.detail = "failed", str(e)
            except Exception as e:
                logger.warning("extract apply failed for %s: %s", op.wiki_id, e)
                op.status, op.detail = "failed", f"{type(e).__name__}: {e}"

        self.store.flush()
        plan.applied = True
        return plan
