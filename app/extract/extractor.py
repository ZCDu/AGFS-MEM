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

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-/.]*|[\u3400-\u9fff]+")
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


def _same_claim(a_words: frozenset[str], b_words: frozenset[str],
                threshold: float = 0.8) -> bool:
    """True when two facts' CONTENT WORDS (numbers aside) are about the same
    claim -- same subject and property, not merely two facts that happen to
    share a word.

    Containment, not Jaccard. The commonest rephrasing is adding or dropping
    the subject — "Leads the retrieval workstream" vs "Alice leads the
    retrieval workstream" — because facts are stored against an entity whose
    name is often implied. Jaccard scores that 0.75 and treats them as
    different; containment scores 1.0, which is the truth: one says
    everything the other says.

    Used two ways: paired with an EQUAL number-set, it means "same claim,
    reworded" (a near-duplicate, see _is_near_duplicate). Paired with a
    DIFFERENT number-set, it means "same claim, updated number" (a real
    correction, see semantica_wrap._detect_fact) -- which is exactly why this
    check had to be split out of _is_near_duplicate rather than inlined
    there: a conflict detector needs the "same claim" half without the
    "numbers must match" half.
    """
    if not a_words or not b_words:
        return a_words == b_words
    shared = len(a_words & b_words)
    containment = shared / min(len(a_words), len(b_words))
    if containment < threshold:
        return False
    # Guard against a short fact being swallowed by a long unrelated one that
    # happens to contain its words. "Leads." is contained in almost anything.
    return max(len(a_words), len(b_words)) - shared <= 2


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
    return _same_claim(a_words, b_words, threshold)

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
      "significance": 0.8,
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
- MEETING MINUTES / PLANS / COMMITMENTS ARE DURABLE. When the source is a
  meeting, plan, decision, or assignment (e.g. a meeting-minutes summary, a
  Q4 planning doc, a roadmap), DO record the concrete numbers and commitments:
  targets and KPIs (user-growth numbers, retention %, GMV), deadlines and
  schedule milestones ("10 mid month", "Q1 end"), the specific work items and
  who owns them, resources decided (budget, headcount, channels), and named
  new features/projects. These are exactly the facts you want to recall later.
  Do NOT discard them as "one-off", "historical", "short-term", or "meeting
  detail". Only truly ephemeral logistics (exact room number, a single
  meeting's idle chit-chat) may be dropped -- but decisions, targets,
  schedules and assignments must be kept.
- NAMED PEOPLE WITH A ROLE ALWAYS GET THEIR OWN ENTITY. If a person is named
  as a lead, owner, manager, or the person responsible for something (a
  project, a task, a decision), create a `person` entity for them -- even if
  the text says nothing else about them beyond the name and role. Never leave
  them as a bare mention inside another entity's fact text (e.g. do not
  settle for a project fact like "led by Zhang Yu" with no Zhang Yu entity).
  Link them to what they own with a relation (e.g. label "leads" or "owns").
- DECISIONS AND COMMITMENTS ALWAYS GET THEIR OWN ENTITY, type "decision".
  When the conversation states that something was decided, approved, or
  committed to (a budget was set, a deadline was agreed, a direction was
  chosen), create a `decision` entity -- do not leave it as a bare fact on
  the meeting or project entity. Link it to whoever made or owns it. This
  matters because a later correction ("actually we reversed that") can only
  be recognized as updating the SAME decision if the decision has its own
  entity to attach to; buried as a fact on an unrelated meeting entity, the
  correction has nothing to find and silently becomes an unrelated,
  contradicting fact instead of a tracked change.
  TITLE IT BY SUBJECT, NOT BY CURRENT VALUE: title the decision after what
  it is a decision ABOUT ("Double 11 promotion budget"), never after the
  specific number or value decided THIS time ("Double 11 promotion budget
  set to 2M"). The value is a fact on the entity, not part of its title. A
  title that bakes in today's number means a later correction gets a
  different-looking title and creates a look-alike duplicate decision
  instead of updating this one -- exactly the failure this rule exists to
  prevent.
- A fact is one atomic claim. Split compound statements.
- confidence: 1.0 stated as fact, 0.7-0.9 implied, below 0.5 do not record.
- significance (0.0-1.0): how much this entity matters to the user's ongoing
  life or work, separate from how confident you are in any one fact about it.
  This controls how long the memory is prioritized once recorded, so vary it
  honestly instead of defaulting every entity to the same number:
    0.9-1.0: the user's own recurring commitments, core projects, direct
             reports or manager, or a decision with real budget/deadline stakes.
    0.6-0.8: the normal case for anything the rules above already say gets its
             own entity -- a named person with an ongoing role, an active
             project, a tracked decision.
    0.3-0.5: real content, but likely to matter only for a while -- a one-off
             event, a minor preference, a peripheral contact.
    0.1-0.2: kept only because it has one concrete fact attached; unlikely to
             come up again.
  Omit the field if genuinely unsure -- it then keeps whatever this entity
  already had (or 0.5 for a brand new one) rather than being forced to guess.
- Prefer attaching facts to entities that were mentioned by name.
- "source" and "target" in relations must exactly match a title in
  "entities", or the name of an entity listed as already known.
- Write summaries and facts as standalone statements. They will be read
  without the conversation for context, so "he said yes" is useless.
- Use the person's own words for preferences and decisions where possible.
- If nothing is worth remembering, return empty lists. That is a valid and
  useful answer.

Relationships (the single most error-prone output - read this carefully):
- The unit that matters is the TOPIC, not the whole message. A single
  message/conversation can genuinely span several unrelated subjects (three
  separate project updates in one status meeting, two different people's
  news dropped into one chat) -- entities from DIFFERENT topics must stay
  unlinked even though they technically share a message.
- WITHIN one topic, co-occurrence is real signal, not noise. If a person,
  project, decision, or event are all part of the same discussed passage --
  the same sentence, paragraph, agenda item, or decision -- link them. You
  do not need an explicit verb for this: appearing together as part of one
  concrete discussion is itself the connection worth recording. Prefer a
  specific label ("leads", "owns", "reports_to", "discussed_in") when the
  text supports one; "related_to" is the correct, acceptable category when
  entities clearly belong to the same discussed topic but no sharper label
  fits -- it is not a catch-all for the whole conversation, only for that
  topic's own entities.
- The test to apply: would someone reading just THAT passage (not the whole
  message) agree these entities belong together? If yes, link them, even
  loosely. If the only thing two entities share is being somewhere in a
  long message that also covers other, different topics, do not link them.
- Still emit NO relation for genuinely incidental co-mentions within a
  topic -- e.g. a calendar reference or a person named only in passing with
  no stated role in that topic's discussion.
- When in doubt about a SPECIFIC label, use "related_to" rather than
  guessing a more specific one. When in doubt about whether two entities
  share a topic AT ALL, do not link them -- a wrong link that looks like a
  fact is worse than no link, since it silently corrupts the graph.
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
    # When this conversation is a genuinely new topic needing a new wiki, the
    # storage scope it will land in and the LLM's proposal for naming it.
    # `target_wiki` starts as the deterministic provisional slug and is
    # replaced by the LLM-finalised one once naming runs (gated on llm_used).
    target_wiki: str = ""
    new_wiki_proposal: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["operations"] = [o.to_dict() for o in self.operations]
        return d


class ConversationExtractor:
    max_tokens: int = 10000

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
             evidence: str | None = None,
             pending_new_wiki: dict | None = None) -> ExtractionPlan:
        """Assess, then extract if worthwhile. Writes nothing.

        `user_id` is the (provisional) storage scope. When `pending_new_wiki`
        is set, this conversation is a genuinely new topic destined for a NEW
        wiki; the name is finalised by the LLM here — after the assessor gate,
        so chatter that never reaches the model does not spend a request on
        naming either.
        """
        assessment = self.assessor.assess(user_id, text)
        allowed = {"store"} if self.min_decision == "store" else {"store", "review"}

        # ---- retraction / removal path ----
        # A message like "forget X", "X is no longer relevant", "remove the
        # fact that Y" is a DELETION, not a new durable fact. The normal
        # extractor treats it as new content to create/add/link (or discards
        # it as text), which is why deletions never landed. When the text is
        # a clear removal intent we build a DELETE plan against the exact
        # wiki + entity that already owns the subject, instead of the
        # assessor-gated create path below.
        from app.intents.retract import is_retraction
        if not force and is_retraction(text):
            removal_plan = self._plan_removal(user_id, text, assessment)
            if removal_plan is not None:
                return removal_plan

        if not force and assessment.decision not in allowed:
            # The cheap filter already answered. No model call.
            return ExtractionPlan(
                decision=assessment.decision,
                assessment=assessment.to_dict(),
                discarded=[f"Assessor returned {assessment.decision!r}: "
                           + "; ".join(assessment.reasons)],
                llm_used=False,
                target_wiki=user_id or "",
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
        plan.target_wiki = user_id or ""
        if truncated_note:
            plan.rejected.append({"reason": truncated_note, "kind": "truncation"})
        if pending_new_wiki is not None:
            # Carry the deterministic provisional name through so the LLM can
            # refine it (and fall back to it on failure).
            plan.new_wiki_proposal = dict(pending_new_wiki)
            self._name_new_wiki(plan, text)
        return plan

    def _plan_removal(self, user_id: str, text: str, assessment) -> ExtractionPlan | None:
        """Build a DELETE/UPDATE plan when the message retracts or corrects
        stored info, via the same Semantica-backed pipeline the interactive
        /chat endpoint uses (app/intents/semantica_crud.py) instead of the
        old anchor-word/keyword heuristic this replaced. The LLM structures
        the claim (entity + property + value, value=null for a pure
        retraction); `_to_store_op` (pure, no writes) decides delete vs
        update vs add from that. Returns None when nothing actionable comes
        back, in which case the caller falls through to the ordinary
        (create/extract) path.

        Still a PLAN, not a write: unlike apply_semantica_conflict() (used by
        /chat, which applies inline), this only builds Operations for
        apply() to execute later, so the interactive /extract review-then-
        apply flow keeps working exactly as it does for every other
        operation type. A destructive delete_entity/delete_fact is marked
        payload["confirm"]=True so apply() stages it for confirmation rather
        than executing it outright -- required for autocapture, which runs
        unattended with no one present to approve a deletion in the moment.
        """
        from app.intents.semantica_crud import (_to_store_op, resolve_entity_id,
                                                structure_claim)

        context = "\n".join(self._known_entities(user_id, assessment))
        try:
            claims = structure_claim(text, context, self.llm, retraction_hint=True)
        except Exception as e:
            logger.warning("claim structuring for retraction failed: %s", e)
            return None
        if not claims:
            return None

        plan = ExtractionPlan(decision=assessment.decision,
                              assessment=assessment.to_dict(),
                              target_wiki=user_id)
        plan.llm_used = True
        for raw_claim in claims:
            if not isinstance(raw_claim, dict):
                continue
            raw_entity = (raw_claim.get("entity") or "").strip()
            entity = resolve_entity_id(self.store, user_id, raw_entity) or raw_entity
            if not entity:
                continue
            claim = {**raw_claim, "entity": entity}
            op = _to_store_op(self.store, user_id, claim, None)
            if op is None:
                continue
            plan.matched_entities.append(entity)
            if op["op"] == "delete_entity":
                plan.operations.append(Operation(
                    op="delete_entity", wiki_id=entity,
                    payload={"entity": entity, "cascade": True,
                             "hard_delete": True, "confirm": True},
                    reason=f"retracted: {entity!r} should be removed from wiki {user_id!r}"))
            elif op["op"] == "delete_fact":
                plan.operations.append(Operation(
                    op="delete_fact", wiki_id=entity,
                    payload={"entity": entity, "fact_id": op["fact_id"],
                             "confirm": True},
                    reason=f"retracted: {entity!r} no longer has this fact"))
            elif op["op"] == "update_fact":
                plan.operations.append(Operation(
                    op="update_fact", wiki_id=entity,
                    payload={"fact_id": op["fact_id"], "text": op["text"],
                             "confidence": op.get("confidence", 0.9)},
                    reason=f"corrected: new statement about {entity!r}"))
            elif op["op"] == "add_fact":
                plan.operations.append(Operation(
                    op="add_fact", wiki_id=entity,
                    payload={"text": op["text"],
                             "confidence": op.get("confidence", 0.9)},
                    reason=f"stated about {entity!r}"))
        if not plan.operations:
            return None
        return plan

    def _name_new_wiki(self, plan: ExtractionPlan, text: str) -> None:
        """Ask the LLM to name the NEW wiki this conversation deserves.

        Runs only after the assessor gate has passed (this method is called
        only from the post-extraction path), so naming never fires for text
        that was not stored. On any LLM failure the deterministic provisional
        name in `plan.target_wiki` is kept -- routing still works, just less
        intelligently named.
        """
        proposal = plan.new_wiki_proposal if isinstance(
            plan.new_wiki_proposal, dict) else {}
        provisional_title = proposal.get("provisional_title") or "New Wiki"
        # Reference set for the LLM's judgment: the OTHER wikis the user
        # already reaches (computed in _resolve_target), NOT the provisional
        # new wiki's own storage scope. Reading the new scope here was a bug:
        # a same-named archived wiki's leftover entities made the LLM think
        # "this topic already exists" and drop the proposal, leaving the
        # crude "New Wiki" fallback.
        existing = proposal.get("existing") or []
        try:
            verdict = self.llm.evaluate_new_topic(text, existing[:30]) or {}
        except Exception as e:
            logger.warning("new-wiki naming unavailable for %s; keeping "
                           "deterministic name: %s", plan.target_wiki, e)
            verdict = {}
        if not verdict.get("is_new_topic"):
            # The LLM judged this as actually belonging to an existing topic.
            # We only honour a "continue" when the deterministic router
            # flagged a name-cited continuation (`query` path, tentative_wiki
            # set): the text cited an entity a real wiki already owns, so the
            # cited wiki is the authoritative home. On the `create` path no
            # name was cited and the router already decided this is a genuinely
            # NEW topic -- the LLM's job there is to NAME it, not to veto its
            # existence based on loose vocabulary overlap. Allowing a vague
            # `belongs_to` to absorb a new named project into an unrelated
            # wiki (e.g. Aurora -> Nimbus because both "migrate X to cloud")
            # corrupts the graph: once the new project's entities land in the
            # wrong wiki, every later reference scores a topical match there.
            # So on `create` we keep the new wiki and only use the model's
            # title; we never re-point into an existing wiki.
            tentative = proposal.get("tentative_wiki")
            is_name_cited = isinstance(tentative, str) and tentative.strip()
            if is_name_cited:
                belongs = verdict.get("belongs_to")
                target = tentative.strip()
                if not target and isinstance(belongs, str) and belongs.strip():
                    target = belongs.strip()
                if target:
                    plan.target_wiki = target
                plan.new_wiki_proposal = None
                return
            # Not a name-cited continuation: this is a genuinely new topic.
            # Keep the wiki; fall through to naming below (the model's title,
            # else the deterministic provisional title).
            verdict = dict(verdict)
            verdict["is_new_topic"] = True
        # Prefer the LLM's name; fall back to the deterministic provisional one
        # if the model named nothing or was unavailable.
        title = (verdict.get("title") or "").strip() \
            or provisional_title or "New Wiki"
        # Curated tags for the new wiki: the model's explicit tags if given,
        # else a cheap derivation from its topic/title so every new wiki enters
        # the router's TAG GATE with usable labels. E.g. topic
        # "search platform migration" -> ["search", "platform", "migration"].
        explicit_tags = [str(t).strip() for t in (verdict.get("tags") or [])
                         if t and str(t).strip()]
        if not explicit_tags:
            from app.verify.assessor import _content_tokens
            topic = str(verdict.get("topic") or "")
            explicit_tags = sorted(_content_tokens(f"{title} {topic}"))[:8]
        plan.new_wiki_proposal = {
            "title": title,
            "description": verdict.get("description") or "",
            "topic": verdict.get("topic") or "",
            "tags": explicit_tags,
            "reason": verdict.get("reason") or "new topic",
        }
        # Re-point the storage scope to the LLM-finalised name so the created
        # wiki and the applied operations share one folder. Always re-slug the
        # LLM's title: the provisional slug from routing is just a cheap
        # stand-in and may differ (e.g. provisional "acme-corp" vs the model's
        # "Acme Corp Deal" -> "acme-corp-deal").
        try:
            from app.wikis.registry import slugify_wiki as _slug
            final_slug = _slug(title)
            if final_slug:
                plan.target_wiki = final_slug
        except Exception:
            pass

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
        # Current significance of entities this plan touches, fetched
        # alongside existing_facts above (same read, no extra cost). Used so
        # a new significance judgement can only raise an entity's standing,
        # never spuriously lower it because this particular pass took a
        # narrower view of one conversation -- see the payload_significance
        # computation below.
        existing_significance: dict[str, float] = {}

        # Titles that are endpoints of a relation in THIS payload. A relation
        # can legitimately reference an entity that carries no inline fact (the
        # fact lives on the other endpoint). We must NOT stub-skip such an
        # entity or the relation loses its target.
        _relation_titles = {
            _normalize(str(r.get("source") or ""))
            for r in (data.get("relations") or [])
        } | {
            _normalize(str(r.get("target") or ""))
            for r in (data.get("relations") or [])
        }

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
            if existing is None:
                # No exact title/alias hit -- try the resolver's fuzzy tier
                # (word-containment, then semantica string-similarity) before
                # concluding this is a new entity. Read-only: find_candidates
                # never writes, so this stays inside plan()'s "writes
                # nothing" contract. A SINGLE strong candidate is treated as
                # the same entity under a variant name; zero or multiple
                # candidates fall through to today's behavior (create new) --
                # an ambiguous multi-candidate case is not auto-merged here.
                candidates = self.resolver.find_candidates(
                    user_id, title, type_hint=type_)
                if len(candidates) == 1:
                    existing = candidates[0]
            facts = [f for f in (raw_entity.get("facts") or [])
                     if isinstance(f, dict) and str(f.get("text") or "").strip()]
            summary = str(raw_entity.get("summary") or "").strip()
            # STUB GATE: never create an ENTITY SHELL with no durable content.
            # An entity the model names but gives no facts, no summary and no
            # relation endpoint is noise ("Alice Johnson", "Project Atlas"
            # with nothing else) -- creating it spawns a wiki full of empty
            # stubs. Only block NEW entities; existing ones are untouched, and
            # a relation-endpoint is kept so edges stay valid. (An entity whose
            # facts were all duplicates is still a legitimate existing node; a
            # genuinely empty new stub adds nothing.)
            if (existing is None
                    and not facts and not summary
                    and _normalize(title) not in _relation_titles):
                plan.rejected.append({
                    "kind": "entity", "value": title,
                    "why": "no facts, summary or relations (empty stub)"})
                continue

            matched = existing is not None and existing.type == type_
            if matched:
                wiki_id = existing.wiki_id
                plan.matched_entities.append(wiki_id)
                if wiki_id not in existing_facts:
                    current = self.store.get_entity(user_id, wiki_id, touch=False)
                    existing_facts[wiki_id] = (
                        [f.text for f in current.facts] if current else [])
                    existing_significance[wiki_id] = (
                        current.metadata.significance if current else 0.5)
            else:
                wiki_id = self.store.compute_wiki_id(type_, title)
                plan.new_entities.append(wiki_id)
            resolved[_normalize(title)] = wiki_id

            aliases = [str(a).strip() for a in (raw_entity.get("aliases") or [])
                       if str(a).strip()][:10]
            summary = str(raw_entity.get("summary") or "").strip()
            # For a MATCHED entity, the upsert must target its own canonical
            # title: upsert_entity recomputes wiki_id from (type, title)
            # itself (app/graph/store.py) rather than taking wiki_id
            # directly, so sending the INCOMING title (an alias, or a
            # fuzzy-matched variant spelling) would recompute a DIFFERENT
            # slug and silently create a second entity instead of merging
            # into the one just matched -- true for the alias-match case
            # even before the fuzzy tier above existed. The incoming title
            # becomes an alias instead, so this exact phrasing resolves
            # directly (no fuzzy pass needed) next time.
            payload_title = existing.title if existing is not None else title
            if (existing is not None and _normalize(title) != _normalize(existing.title)
                    and title not in aliases):
                aliases = aliases + [title]

            # significance: honest per-pass judgement, clamped to [0,1].
            # Missing/unparseable -> None, which leaves an existing entity's
            # significance untouched and lets a new one fall back to the
            # store's 0.5 default (see upsert_entity). For an entity that
            # already exists, a lower judgement from THIS pass never pulls
            # significance down -- only reinforcement (seeing it again,
            # rated at least as high) can raise it further.
            try:
                raw_sig = raw_entity.get("significance")
                payload_significance = float(raw_sig) if raw_sig is not None else None
            except (TypeError, ValueError):
                payload_significance = None
            if payload_significance is not None:
                payload_significance = max(0.0, min(1.0, payload_significance))
                if matched:
                    payload_significance = max(
                        payload_significance, existing_significance.get(wiki_id, 0.5))

            plan.operations.append(Operation(
                op="upsert_entity", wiki_id=wiki_id,
                payload={"type": type_, "title": payload_title, "aliases": aliases,
                         "summary_append": summary or None,
                         "significance": payload_significance},
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
        abandon the rest, and every outcome is recorded on the operation.

        Consecutive add_fact operations targeting the SAME entity are batched
        into one store call (see EntityGraphStore.add_facts) instead of one
        round-trip per fact -- measured as the dominant cost in capture
        latency, well above the LLM call that produced the facts: under
        OKF_MODE=companion each add_fact() is a full read-modify-write (1 GET
        + 2 PUTs), so an entity proposed with 9 facts meant 27 sequential
        network round trips. _build_plan() already emits one entity's
        add_fact operations back-to-back (right after its upsert_entity,
        before the next entity starts), so grouping "same wiki_id, same op,
        consecutive" catches the common case without reordering anything
        relative to other operation types.
        """
        ops = plan.operations
        i, n = 0, len(ops)
        while i < n:
            op = ops[i]
            if op.status == "rejected":
                i += 1
                continue
            if op.op == "add_fact":
                j = i
                batch: list[Operation] = []
                while (j < n and ops[j].op == "add_fact"
                      and ops[j].status != "rejected" and ops[j].wiki_id == op.wiki_id):
                    batch.append(ops[j])
                    j += 1
                self._apply_add_fact_batch(user_id, op.wiki_id, batch)
                i = j
                continue
            try:
                if op.op == "upsert_entity":
                    p = op.payload
                    self.store.upsert_entity(
                        user_id, p["type"], p["title"],
                        aliases=p.get("aliases") or None,
                        summary_append=p.get("summary_append"),
                        significance=p.get("significance"),
                        # Variant spellings should merge into what exists
                        # rather than 409 — the model producing "ALICE chen"
                        # for a known "Alice Chen" is expected, not an error.
                        on_conflict="disambiguate")
                elif op.op == "link_entities":
                    p = op.payload
                    self.store.link_entities(
                        user_id, op.wiki_id, p["target_wiki_id"],
                        category=p.get("category", "related_to"),
                        label=p.get("label", ""), reason=op.reason)
                elif op.op == "delete_entity":
                    if op.payload.get("confirm"):
                        # A retraction-driven whole-entity delete is
                        # destructive and this path can run unattended
                        # (autocapture) -- stage it instead of executing, the
                        # same "confirm before forget" mechanism /chat uses
                        # (app/intents/semantica_crud.py). Reviewed via the
                        # existing GET/POST .../intents/deletions/* endpoints.
                        from app.intents.semantica_crud import stage_pending_deletion
                        pending_id, _ = stage_pending_deletion(
                            self.store, user_id, op.wiki_id, op.payload["entity"],
                            op="delete_entity")
                        op.status = "needs_confirmation"
                        op.detail = f"staged as pending deletion {pending_id}"
                        i += 1
                        continue
                    # Removal intents ("forget X", "X is no longer relevant")
                    # surface as a delete_entity op. We hard-delete by default
                    # so a retracted memory does not linger as a tombstone.
                    ok = self.store.delete_entity(
                        user_id, op.wiki_id,
                        cascade=bool(op.payload.get("cascade", True)),
                        hard_delete=bool(op.payload.get("hard_delete", True)),
                    )
                    if ok:
                        op.status, op.detail = "applied", "deleted"
                    else:
                        # The model often emits a removal for a ghost entity
                        # it never defined (a hallucinated "user", a misparsed
                        # name). That is a no-op, not a success -- label it
                        # skipped so the UI never reports a phantom removal as
                        # if it changed something.
                        op.status, op.detail = "skipped", "entity not found; nothing to delete"
                        i += 1
                        continue
                elif op.op == "delete_fact":
                    p = op.payload
                    if p.get("confirm"):
                        from app.intents.semantica_crud import stage_pending_deletion
                        pending_id, _ = stage_pending_deletion(
                            self.store, user_id, op.wiki_id, p["entity"],
                            op="delete_fact", fact_id=p["fact_id"])
                        op.status = "needs_confirmation"
                        op.detail = f"staged as pending deletion {pending_id}"
                        i += 1
                        continue
                    self.store.remove_fact(user_id, op.wiki_id, p["fact_id"])
                    op.detail = f"fact {p['fact_id']} deleted from {op.wiki_id}"
                elif op.op == "update_fact":
                    p = op.payload
                    self.store.update_fact(user_id, op.wiki_id, p["fact_id"],
                                           text=p.get("text"),
                                           confidence=p.get("confidence"))
                    op.detail = f"fact {p['fact_id']} updated on {op.wiki_id}"
                else:
                    op.status, op.detail = "failed", f"unknown operation {op.op!r}"
                    i += 1
                    continue
                op.status = "applied"
            except SlugConflictError as e:
                op.status, op.detail = "failed", str(e)
            except Exception as e:
                logger.warning("extract apply failed for %s: %s", op.wiki_id, e)
                op.status, op.detail = "failed", f"{type(e).__name__}: {e}"
            i += 1

        self.store.flush()
        plan.applied = True
        return plan

    def _apply_add_fact_batch(self, user_id: str, wiki_id: str,
                              batch: list[Operation]) -> None:
        """Apply a run of consecutive add_fact operations for ONE entity as a
        single store call. Falls back to applying them one at a time (the
        old behavior) if the batch call itself raises, so one malformed
        payload in a batch of 8 doesn't fail all 8 -- preserving apply()'s
        "one failure does not abandon the rest" contract at the per-fact
        level even though the happy path is now one round trip."""
        try:
            self.store.add_facts(user_id, wiki_id, [
                {"text": op.payload["text"],
                 "confidence": op.payload.get("confidence", 1.0),
                 "evidence": op.payload.get("evidence") or []}
                for op in batch
            ])
            for op in batch:
                op.status = "applied"
        except Exception as e:
            logger.warning("batched add_facts failed for %s (%d fact(s)), "
                           "falling back to one at a time: %s",
                           wiki_id, len(batch), e)
            for op in batch:
                try:
                    self.store.add_fact(user_id, op.wiki_id, op.payload["text"],
                                        confidence=op.payload.get("confidence", 1.0),
                                        evidence=op.payload.get("evidence") or [])
                    op.status = "applied"
                except SlugConflictError as e2:
                    op.status, op.detail = "failed", str(e2)
                except Exception as e2:
                    op.status, op.detail = "failed", f"{type(e2).__name__}: {e2}"
