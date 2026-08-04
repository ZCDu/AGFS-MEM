"""
Extraction layer tests.

All of these run against a STUB model. A layer that can only be tested with a
live API key does not get tested — it costs money, needs network, and gives
different answers every run. The LLM client is injectable precisely so the
logic around it can be pinned deterministically.

What is worth pinning is mostly the distrust: that the assessor gates the
model call, that a plan writes nothing, that invalid output is dropped rather
than passed through, and that extracted entities attach to existing ones
instead of quietly duplicating them.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from app.extract.extractor import ConversationExtractor
from app.extract.llm import LLMError, LLMNotConfigured, extract_json
from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend


class StubLLM:
    """Returns a canned response and records how often it was called."""

    def __init__(self, response: str | dict | Exception = None):
        if isinstance(response, dict):
            response = json.dumps(response)
        self.response = response
        self.calls = 0
        self.last_prompt = ""

    configured = True

    def complete(self, system: str, user: str, **kw) -> str:
        self.calls += 1
        self.last_prompt = user
        if isinstance(self.response, Exception):
            raise self.response
        return self.response or "{}"


CONVERSATION = (
    "We decided today to move Orion off the legacy index. Alice Chen will "
    "lead the migration and the deadline is 2026-08-15. She prefers async "
    "written updates over status meetings."
)

GOOD_RESPONSE = {
    "entities": [
        {"title": "Alice Chen", "type": "person", "aliases": ["Alice"],
         "summary": "Engineer leading the Orion index migration.",
         "facts": [{"text": "Leads the Orion index migration.", "confidence": 0.95}]},
        {"title": "Orion", "type": "project",
         "summary": "Search platform migrating off the legacy index.",
         "facts": [{"text": "Migration deadline is 2026-08-15.", "confidence": 0.9}]},
    ],
    "relations": [
        {"source": "Alice Chen", "target": "Orion",
         "category": "related_to", "label": "leads", "reason": "stated"},
    ],
    "discarded": ["Greetings at the start of the conversation."],
}


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend)
    backend.close()


# ---------- the assessor gate ----------

def test_chatter_never_reaches_the_model(store):
    """PLAN.md §12: do not spend a model call deciding whether something
    matters when cheap signals already answer that."""
    llm = StubLLM(GOOD_RESPONSE)
    plan = ConversationExtractor(store, llm).plan("u", "hey thanks so much, sounds good")

    assert llm.calls == 0, "the assessor should have stopped this before the LLM"
    assert plan.llm_used is False
    assert plan.operations == []
    assert plan.discarded, "it should say why it stopped"


def test_force_overrides_the_gate(store):
    llm = StubLLM(GOOD_RESPONSE)
    ConversationExtractor(store, llm).plan("u", "hey thanks", force=True)
    assert llm.calls == 1


def test_min_decision_store_is_stricter(store):
    """'review' spends calls on borderline text; 'store' does not."""
    borderline = "The Helios rollout slipped again this week, second time."
    lenient = ConversationExtractor(store, StubLLM(GOOD_RESPONSE), min_decision="review")
    strict_llm = StubLLM(GOOD_RESPONSE)
    strict = ConversationExtractor(store, strict_llm, min_decision="store")

    lenient_plan = lenient.plan("u", borderline)
    strict.plan("u", borderline)
    if lenient_plan.decision == "review":
        assert strict_llm.calls == 0, "'store' should not spend a call on 'review' text"


# ---------- plan writes nothing ----------

def test_plan_does_not_touch_the_graph(store):
    llm = StubLLM(GOOD_RESPONSE)
    plan = ConversationExtractor(store, llm).plan("u", CONVERSATION)

    assert llm.calls == 1
    assert plan.operations, "it should have proposed something"
    assert store.list_entities("u") == [], "planning must not write"
    assert plan.applied is False


def test_apply_writes_what_the_plan_proposed(store):
    ex = ConversationExtractor(store, StubLLM(GOOD_RESPONSE))
    plan = ex.apply("u", ex.plan("u", CONVERSATION))
    store.flush()

    ids = set(store.list_entities("u"))
    assert {"person/alice-chen", "project/orion"} <= ids
    assert all(o.status == "applied" for o in plan.operations), \
        [o.to_dict() for o in plan.operations if o.status != "applied"]

    alice = store.get_entity("u", "person/alice-chen", touch=False)
    assert any("migration" in f.text for f in alice.facts)
    assert "Alice" in alice.aliases
    assert [r.target for r in alice.relations] == ["project/orion"]


# ---------- distrust of the model ----------

def test_extracted_entities_attach_to_existing_ones(store):
    """Without this the layer silently duplicates memory on every run, which
    is worse than an error because nothing surfaces it."""
    store.upsert_entity("u", "person", "Alice Chen", aliases=["Alice"],
                        summary_append="Staff engineer.")
    store.flush()

    ex = ConversationExtractor(store, StubLLM(GOOD_RESPONSE))
    plan = ex.plan("u", CONVERSATION)

    assert "person/alice-chen" in plan.matched_entities
    assert "person/alice-chen" not in plan.new_entities

    ex.apply("u", plan)
    store.flush()
    assert len([w for w in store.list_entities("u") if w.startswith("person/")]) == 1


def test_alias_in_the_model_output_still_resolves(store):
    store.upsert_entity("u", "person", "Alice Chen", aliases=["Alice"])
    store.flush()
    response = {"entities": [{"title": "Alice", "type": "person",
                              "summary": "Referred to by first name."}],
                "relations": []}
    plan = ConversationExtractor(store, StubLLM(response)).plan("u", CONVERSATION)
    assert plan.matched_entities == ["person/alice-chen"]


def test_invalid_types_and_categories_are_dropped(store):
    response = {
        "entities": [
            {"title": "Valid", "type": "concept", "summary": "ok"},
            {"title": "Bogus", "type": "spaceship", "summary": "invented type"},
            {"title": "!!!", "type": "concept", "summary": "unusable title"},
        ],
        "relations": [
            {"source": "Valid", "target": "Bogus", "category": "related_to"},
            {"source": "Valid", "target": "Valid", "category": "related_to"},
        ],
    }
    plan = ConversationExtractor(store, StubLLM(response)).plan("u", CONVERSATION)

    created = {o.payload.get("title") for o in plan.operations if o.op == "upsert_entity"}
    assert created == {"Valid"}
    whys = " ".join(r["why"] for r in plan.rejected)
    assert "invalid type" in whys
    assert "unusable title" in whys
    assert "self-referential" in whys


def test_relations_to_undefined_entities_are_dropped(store):
    """Guessing the endpoint is how a graph silently acquires wrong edges."""
    response = {
        "entities": [{"title": "Orion", "type": "project", "summary": "x"}],
        "relations": [{"source": "Orion", "target": "Someone Never Mentioned",
                       "category": "related_to", "label": "involves"}],
    }
    plan = ConversationExtractor(store, StubLLM(response)).plan("u", CONVERSATION)

    assert not [o for o in plan.operations if o.op == "link_entities"]
    assert any("endpoint not among" in r["why"] for r in plan.rejected)


def test_confidence_is_clamped(store):
    response = {"entities": [{"title": "Orion", "type": "project", "summary": "x",
                              "facts": [{"text": "a", "confidence": 7.5},
                                        {"text": "b", "confidence": -2},
                                        {"text": "c", "confidence": "nonsense"}]}],
                "relations": []}
    plan = ConversationExtractor(store, StubLLM(response)).plan("u", CONVERSATION)
    confs = [o.payload["confidence"] for o in plan.operations if o.op == "add_fact"]
    assert all(0.0 <= c <= 1.0 for c in confs), confs


def test_empty_extraction_is_a_valid_answer(store):
    plan = ConversationExtractor(
        store, StubLLM({"entities": [], "relations": [],
                        "discarded": ["nothing durable here"]})).plan("u", CONVERSATION)
    assert plan.operations == []
    assert plan.discarded == ["nothing durable here"]


def test_known_entities_are_offered_to_the_model(store):
    """So it reuses existing titles instead of inventing near-duplicates."""
    store.upsert_entity("u", "project", "Orion", summary_append="Search platform.")
    store.flush()
    llm = StubLLM(GOOD_RESPONSE)
    ConversationExtractor(store, llm).plan("u", CONVERSATION)
    assert "Orion" in llm.last_prompt
    assert "already in memory" in llm.last_prompt


def test_one_failing_operation_does_not_abandon_the_rest(store):
    ex = ConversationExtractor(store, StubLLM(GOOD_RESPONSE))
    plan = ex.plan("u", CONVERSATION)
    plan.operations.insert(1, type(plan.operations[0])(
        op="not_a_real_op", wiki_id="person/alice-chen", payload={}))

    ex.apply("u", plan)
    statuses = [o.status for o in plan.operations]
    assert "failed" in statuses and "applied" in statuses


# ---------- the client ----------

def test_json_survives_the_ways_models_wrap_it():
    want = {"entities": [], "relations": []}
    for wrapped in (
        json.dumps(want),
        f"```json\n{json.dumps(want)}\n```",
        f"```\n{json.dumps(want)}\n```",
        f"Sure, here you go:\n\n{json.dumps(want)}\n\nHope that helps.",
    ):
        assert extract_json(wrapped) == want


def test_unparseable_response_raises_clearly():
    for bad in ("", "no json at all", "{unclosed"):
        with pytest.raises(LLMError):
            extract_json(bad)


def test_json_with_braces_inside_strings_is_parsed():
    payload = {"entities": [{"title": "The {weird} name", "type": "concept",
                             "summary": "has braces"}], "relations": []}
    assert extract_json(f"here: {json.dumps(payload)}") == payload


def test_missing_api_key_is_reported_not_crashed():
    from app.extract.llm import LLMClient
    client = LLMClient(api_key=None, base_url="https://example.invalid/v1",
                       model="whatever")
    assert client.configured is False
    with pytest.raises(LLMNotConfigured):
        client.complete("system", "user")


# ---------- over the API ----------

def test_extract_returns_503_without_a_key(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    import app.config as cfg
    import app.deps as deps
    cfg._settings = None
    deps.get_llm_client.cache_clear()

    r = client.post("/v1/users/demo/extract", json={"text": CONVERSATION})
    assert r.status_code == 503
    assert "API key" in r.json()["detail"]


def test_extract_apply_validates_submitted_operations(client):
    """This endpoint is reachable without ever calling /extract, so operations
    cannot be trusted just because they look like a plan."""
    bad = client.post("/v1/users/demo/extract/apply",
                      json={"operations": [{"op": "rm -rf", "wiki_id": "x", "payload": {}}]})
    assert bad.status_code == 422

    incomplete = client.post("/v1/users/demo/extract/apply",
                             json={"operations": [{"op": "add_fact",
                                                   "wiki_id": "person/x", "payload": {}}]})
    assert incomplete.status_code == 422
    assert "missing" in incomplete.json()["detail"]


def test_extract_apply_writes(client):
    r = client.post("/v1/users/demo/extract/apply", json={"operations": [
        {"op": "upsert_entity", "wiki_id": "person/nadia",
         "payload": {"type": "person", "title": "Nadia", "summary_append": "A novelist."}},
        {"op": "add_fact", "wiki_id": "person/nadia",
         "payload": {"text": "Writes magical realism.", "confidence": 0.9}},
    ]})
    assert r.status_code == 200
    assert r.json()["applied"] == 2
    assert client.get("/v1/users/demo/wiki/person/nadia").status_code == 200


def test_reextracting_the_same_conversation_does_not_duplicate_facts(store):
    """This layer runs repeatedly over overlapping conversations and add_fact
    appends unconditionally, so without dedup an entity fills with copies of
    the same claim."""
    ex = ConversationExtractor(store, StubLLM(GOOD_RESPONSE))
    for _ in range(3):
        ex.apply("u", ex.plan("u", CONVERSATION))
        store.flush()

    alice = store.get_entity("u", "person/alice-chen", touch=False)
    texts = [f.text for f in alice.facts]
    assert len(texts) == len(set(texts)), f"duplicated facts: {texts}"
    assert len(alice.relations) == 1


def test_a_corrected_fact_is_still_recorded(store):
    """Dedup must not swallow updates. Two statements about the same subject
    with different content are different facts, and losing the newer one
    defeats the purpose of extracting corrections."""
    first = {"entities": [{"title": "Orion", "type": "project", "summary": "x",
                           "facts": [{"text": "Deadline is 2026-08-15.", "confidence": 1.0}]}],
             "relations": []}
    second = {"entities": [{"title": "Orion", "type": "project", "summary": "x",
                            "facts": [{"text": "Deadline is 2026-09-01.", "confidence": 1.0}]}],
              "relations": []}

    ex = ConversationExtractor(store, StubLLM(first))
    ex.apply("u", ex.plan("u", CONVERSATION))
    store.flush()

    ex2 = ConversationExtractor(store, StubLLM(second))
    ex2.apply("u", ex2.plan("u", CONVERSATION))
    store.flush()

    texts = [f.text for f in store.get_entity("u", "project/orion", touch=False).facts]
    assert any("08-15" in t for t in texts)
    assert any("09-01" in t for t in texts), "the correction was swallowed by dedup"


def test_unrecognised_top_level_keys_are_reported_not_dropped(store):
    """The schema nests facts inside entities. A model that emits a top-level
    "facts" array instead is not obviously wrong, and JSON mode will not stop
    it — so that content must be reported rather than silently discarded.

    Silent loss that looks like success is the worst failure mode available to
    a memory system: nothing in the plan, nothing in the logs, and the user
    believes the conversation was captured.
    """
    llm = StubLLM({
        "entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer."}],
        "facts": [{"entity": "Alice Chen", "text": "Would have been lost."}],
        "notes": "also unread",
    })
    extractor = ConversationExtractor(store, llm)
    plan = extractor.plan(
        "u", "We decided today that Alice Chen will lead the migration, "
             "and the deadline is 2026-08-15.")

    reported = " ".join(r["reason"] for r in plan.rejected)
    assert "'facts'" in reported, "a dropped top-level facts array must be reported"
    assert "'notes'" in reported
    assert [o.op for o in plan.operations] == ["upsert_entity"]


def test_well_formed_output_reports_nothing_rejected(store):
    """The guard must not fire on correct output."""
    llm = StubLLM({
        "entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer.",
                      "facts": [{"text": "Leads retrieval.", "confidence": 0.9}]}],
        "relations": [],
        "discarded": [],
    })
    plan = ConversationExtractor(store, llm).plan(
        "u", "We decided today that Alice Chen will lead the migration, "
             "and the deadline is 2026-08-15.")
    assert plan.rejected == []
    assert {o.op for o in plan.operations} == {"upsert_entity", "add_fact"}


# ---------- duplicate facts across repeated extraction ----------

def test_near_duplicate_detection_ignores_phrasing_but_never_numbers():
    """Exact-text matching was not enough: the model rephrases the same claim
    between runs, so re-extracting an overlapping conversation accumulated
    near-identical facts.

    Any difference in NUMBERS blocks the merge, whatever the prose similarity.
    A duplicate is untidy; a silently swallowed correction is wrong.
    """
    from app.extract.extractor import _is_near_duplicate as dup

    # Same claim, different wording — the subject being named or implied is
    # the commonest variation, since facts hang off an entity.
    assert dup("Leads the retrieval workstream.", "Alice leads the retrieval workstream.")
    assert dup("Leads the retrieval workstream.", "She leads the retrieval workstream.")
    assert dup("The deadline is 2026-08-15.", "Deadline is 2026-08-15.")

    # Different numbers: corrections and version bumps must stay separate.
    assert not dup("The deadline is 2026-08-15.", "The deadline is 2026-09-01.")
    assert not dup("Joined Northwind in 2024.", "Joined Northwind in 2023.")

    # Negation must never be treated as filler.
    assert not dup("Uses BM25 for lexical search.", "Does not use BM25 for lexical search.")

    # A short fact must not be swallowed by a longer one containing its words.
    assert not dup("Leads retrieval.",
                   "Leads retrieval, mentors juniors, runs on-call and owns budget.")
    assert not dup("Based in Taipei.", "Based in Taipei and travels to Tokyo monthly.")

    assert not dup("Leads the retrieval team.", "Works on the billing system.")


def test_re_extracting_the_same_conversation_adds_nothing(store):
    """The reported flaw: applying extraction repeatedly stored the same fact
    again each time."""
    payload = {"entities": [{"type": "person", "title": "Alice Chen",
                             "summary": "Staff engineer.",
                             "facts": [{"text": "Leads the retrieval workstream.",
                                        "confidence": 0.9}]}],
               "relations": [], "discarded": []}
    extractor = ConversationExtractor(store, StubLLM(payload))
    text = ("We decided today that Alice Chen leads the retrieval workstream "
            "and the deadline is 2026-08-15.")

    extractor.apply("u", extractor.plan("u", text))
    store.flush()
    first = len(store.get_entity("u", "person/alice-chen", touch=False).facts)

    extractor.apply("u", extractor.plan("u", text))
    store.flush()
    second = len(store.get_entity("u", "person/alice-chen", touch=False).facts)

    assert first == second == 1, "re-extraction must not duplicate the fact"


def test_rephrased_fact_is_skipped_but_a_new_one_is_kept(store):
    """The realistic case: a later run rephrases what is known and adds
    something genuinely new. Only the new part should land."""
    first = {"entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer.",
                           "facts": [{"text": "Leads the retrieval workstream."}]}],
             "relations": [], "discarded": []}
    second = {"entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer.",
                            "facts": [{"text": "Alice leads the retrieval workstream."},
                                      {"text": "Based in Taipei."}]}],
              "relations": [], "discarded": []}

    llm = StubLLM(first)
    extractor = ConversationExtractor(store, llm)
    text = ("We decided today that Alice Chen leads the retrieval workstream "
            "and the deadline is 2026-08-15.")
    extractor.apply("u", extractor.plan("u", text))
    store.flush()

    llm.response = json.dumps(second)
    plan = extractor.apply("u", extractor.plan("u", text))
    store.flush()

    facts = [f.text for f in store.get_entity("u", "person/alice-chen", touch=False).facts]
    assert len(facts) == 2, f"expected the rephrasing to be skipped, got {facts}"
    assert "Based in Taipei." in facts
    assert any(r.get("kind") == "fact" for r in plan.rejected), \
        "the skipped rephrasing should be reported, not silently dropped"


def test_a_corrected_value_is_stored_as_a_separate_fact(store):
    """Corrections are the reason numeric differences block the merge. Losing
    one would be far worse than keeping a duplicate."""
    original = {"entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer.",
                              "facts": [{"text": "The deadline is 2026-08-15."}]}],
                "relations": [], "discarded": []}
    corrected = {"entities": [{"type": "person", "title": "Alice Chen", "summary": "Engineer.",
                               "facts": [{"text": "The deadline is 2026-09-01."}]}],
                 "relations": [], "discarded": []}

    llm = StubLLM(original)
    extractor = ConversationExtractor(store, llm)
    text = ("We decided today that Alice Chen leads the retrieval workstream "
            "and the deadline is 2026-08-15.")
    extractor.apply("u", extractor.plan("u", text))
    store.flush()

    llm.response = json.dumps(corrected)
    extractor.apply("u", extractor.plan("u", text))
    store.flush()

    facts = [f.text for f in store.get_entity("u", "person/alice-chen", touch=False).facts]
    assert len(facts) == 2, "a changed date must not be merged away as a duplicate"


# ---------- truncated responses ----------

def test_truncation_is_named_not_reported_as_bad_json():
    """A reply cut off at max_tokens is incomplete, not malformed. Reporting
    it as "No JSON object found" sent people hunting for a prompt or schema
    problem when the fix is more tokens or less input."""
    from app.extract.llm import LLMTruncated

    e = LLMTruncated('{"entities": [{"title": "Huaifeng"', 2000)
    msg = str(e)
    assert "ran out of room" in msg
    assert "max_tokens=2000" in msg
    assert "LLM_MAX_TOKENS" in msg, "the error must say how to fix it"


def test_salvage_recovers_complete_objects_from_a_cut_off_reply():
    """Truncation normally lands inside the last entity, leaving several
    finished ones behind. Discarding them wastes a call already paid for and
    forces a retry that may truncate again."""
    from app.extract.llm import salvage_truncated_json

    cut = ('{"entities": ['
           '{"title": "Huaifeng", "type": "organization", "summary": "Unified nation."},'
           '{"title": "Trisnia", "type": "organization", "summary": "Former patron."},'
           '{"title": "Sanrii", "type": "organization", "summary": "Defeated empi')
    got = salvage_truncated_json(cut)

    assert [e["title"] for e in got["entities"]] == ["Huaifeng", "Trisnia"]
    assert salvage_truncated_json("not json at all") is None


def test_plan_salvages_a_truncated_extraction_and_says_so(store):
    """The user should get the entities that did come back, plus a clear note
    that the rest is missing — not a bare failure."""
    from app.extract.llm import LLMClient, LLMTruncated

    cut = ('{"entities": ['
           '{"title": "Huaifeng", "type": "organization", "summary": "Unified nation."},'
           '{"title": "Trisnia", "type": "organization", "summary": "Former pat')

    class Truncating(LLMClient):
        configured = True

        def __init__(self):
            super().__init__(api_key="k", base_url="http://x", model="m")

        def complete(self, system, user, **kw):
            raise LLMTruncated(cut, 2000)

    plan = ConversationExtractor(store, Truncating()).plan(
        "u", "We decided today that Huaifeng was unified after the war ended "
             "on 2026-08-15, with Trisnia as its former patron.")

    assert [o.wiki_id for o in plan.operations] == ["organization/huaifeng"], \
        "the one complete entity should still be proposed"
    notes = [r for r in plan.rejected if r.get("kind") == "truncation"]
    assert notes and "ran out of room" in notes[0]["reason"]
    assert "LLM_MAX_TOKENS" in notes[0]["reason"]


def test_invalid_entity_types_are_reported(store):
    """"nation" is not a valid type. The entity must be rejected with a reason,
    not silently dropped or coerced."""
    llm = StubLLM({"entities": [
        {"title": "Huaifeng", "type": "nation", "summary": "A unified nation."},
        {"title": "Chairwoman Yin", "type": "person", "summary": "Leads the South."}],
        "relations": [], "discarded": []})

    plan = ConversationExtractor(store, llm).plan(
        "u", "We decided today that Huaifeng is led by Chairwoman Yin since 2026-08-15.")

    assert [o.wiki_id for o in plan.operations] == ["person/chairwoman-yin"]
    assert any("invalid type 'nation'" in str(r) for r in plan.rejected)
