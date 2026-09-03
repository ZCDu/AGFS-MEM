"""
Tests for the non-LLM conversation assessor (PLAN.md §4 steps 1-2).

These pin CLASSIFICATION OUTCOMES rather than exact scores. The weights are
tunable and will move; what must not regress is that redundant restatements
get rejected, that durable first-person facts survive, and that chatter does
not outrank them.
"""

from __future__ import annotations

import pytest

from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend
from app.verify.assessor import ConversationAssessor


@pytest.fixture()
def assessor(tmp_path):
    backend = MirageBackend.from_disk(root=str(tmp_path / "bucket"))
    store = EntityGraphStore(backend)
    store.upsert_entity("u", "person", "Alice Chen", aliases=["Alice"],
                        summary_append="Staff engineer on the retrieval team in Taipei.")
    store.upsert_entity("u", "project", "Orion",
                        summary_append="Internal semantic search platform, currently in beta.")
    store.upsert_entity("u", "concept", "Hybrid Search",
                        summary_append="Combines lexical BM25 with dense vector scoring.")
    store.flush()
    yield ConversationAssessor(store)
    backend.close()


# ---------- rejection ----------

def test_short_filler_is_skipped(assessor):
    r = assessor.assess("u", "hey! thanks so much, sounds good to me")
    assert r.decision == "skip"


def test_long_chatter_without_entities_is_skipped(assessor):
    r = assessor.assess("u", "So anyway I was thinking about the weather today and it "
                             "is quite nice outside, maybe we should go for a walk later "
                             "or something like that if you want to")
    assert r.decision == "skip", r.reasons


def test_restating_known_facts_is_rejected_as_redundant(assessor):
    """The §4 redundancy check: text already covered by an entity's summary
    must not be stored again."""
    r = assessor.assess("u", "Alice Chen is a staff engineer on the retrieval team "
                             "based in Taipei.")
    assert r.decision == "skip"
    assert any("redundant" in x for x in r.reasons), r.reasons
    assert r.novelty < 0.3
    assert r.relevance > 0.8, "it should still recognise WHO this is about"


# ---------- acceptance ----------

def test_decision_with_entities_and_deadline_is_stored(assessor):
    r = assessor.assess("u", "We decided today to move Orion off the legacy index. "
                             "Alice will lead the migration and the deadline is "
                             "2026-08-15. Expected p95 latency drops from 800ms to 300ms.")
    assert r.decision == "store"
    linked = {x.wiki_id for x in r.related}
    assert "project/orion" in linked
    assert "person/alice-chen" in linked, "alias 'Alice' should resolve"
    assert "decision" in r.signals["importance_markers"]
    assert r.key_sentences, "a summariser needs somewhere to start"


def test_new_topic_with_unknown_entities_is_stored(assessor):
    """Relevance is 0 by definition for genuinely new subject matter, so this
    only passes if discovery of unknown entities counts for something."""
    r = assessor.assess("u", "Marcus Webb just joined from Acme Corp to run the Helios "
                             "infrastructure migration. He reports to Priya Raman.")
    assert r.decision == "store"
    assert r.relevance == 0.0
    assert len(r.new_candidates) >= 3
    assert "Marcus Webb" in r.new_candidates


def test_short_first_person_preference_survives_the_length_gate(assessor):
    """No named entities, no numbers, eleven words — it scores below every
    threshold, and it is exactly what a long-term memory should keep."""
    r = assessor.assess("u", "I really prefer async written updates over status "
                             "meetings, always have.")
    assert r.decision == "store", r.reasons
    assert "preference" in r.signals["importance_markers"]


def test_short_relational_identity_fact_survives_the_length_gate(assessor):
    """"Ravi is a data engineer at Silverline Analytics" is 7-11 words and uses
    none of the literal identity verbs ("works at", "reports to", ...). It is a
    genuine, durable relational fact and must not be discarded as chatter by
    the min_words gate."""
    r = assessor.assess("u", "Ravi Shah is a data engineer at Silverline "
                             "Analytics in Austin.")
    assert r.decision != "skip", r.reasons
    assert len(r.new_candidates) >= 1  # Ravi Shah is proposed as a new entity


def test_short_relational_identity_fact_worker_in_org(assessor):
    """The earlier 'Anna is a worker in Taldic Corps' case — 7 words, no literal
    identity verb, but a real durable fact."""
    r = assessor.assess("u", "Anna is a worker in Taldic Corps.")
    assert r.decision != "skip", r.reasons


def test_relational_identity_regex_does_not_fire_on_chatter(assessor):
    """Preference and decision phrases must not get identity status just for
    containing 'is a'."""
    for txt in [
        "I prefer async updates over meetings.",
        "We decided to use Postgres for this.",
        "the espresso machine is a thing in the office.",
    ]:
        r = assessor.assess("u", txt)
        # these should NOT be promoted purely by identity; they stay short-skipped
        # or chatter (any non-store decision is fine — the bug was over-promoting)
        assert "identity" not in r.signals.get("importance_markers", []), txt


def test_correction_about_known_entity_is_stored(assessor):
    r = assessor.assess("u", "Actually I was wrong earlier - Hybrid Search does not use "
                             "BM25 anymore, it moved to SPLADE in Q2.")
    assert r.decision == "store"
    assert {x.wiki_id for x in r.related} == {"concept/hybrid-search"}
    assert r.novelty > 0.5, "new information about a known entity"


# ---------- signal precision ----------

def test_markers_match_on_word_boundaries(assessor):
    """Substring matching made 'something like that' a preference statement,
    which promoted chatter above real preferences."""
    r = assessor.assess("u", "It was something like that, unlikely to matter, and we "
                             "walked along the river for a while in the afternoon.")
    assert "preference" not in r.signals.get("importance_markers", {})


def test_sentence_initial_capitals_are_not_treated_as_entities(assessor):
    r = assessor.assess("u", "The deadline moved. Another change happened. Something "
                             "else occurred later on in the week as well.")
    assert "The" not in r.new_candidates
    assert "Another" not in r.new_candidates


def test_assessment_is_read_only(assessor):
    before = set(assessor.store.list_entities("u"))
    assessor.assess("u", "Marcus Webb joined Acme Corp to run the Helios migration "
                         "and reports to Priya Raman.")
    assert set(assessor.store.list_entities("u")) == before, "assess must not write"


def test_every_assessment_explains_itself(assessor):
    for text in ("hi there thanks a lot",
                 "We decided to ship Orion next Tuesday after the review.",
                 "Alice Chen is a staff engineer on the retrieval team in Taipei."):
        r = assessor.assess("u", text)
        assert r.reasons, f"no explanation for {text!r}"
        assert r.decision in {"store", "review", "skip"}


# ---------- name-fragment / first-name retrieval ----------

def test_first_name_lookup_finds_full_name_entity(assessor):
    """A first-name fragment ("whos Anton") must link to the full "Anton
    Lokhy" entity even when no alias covers that single word. The alias tier
    below proves Alice resolves via her alias; Anton proves the fragment tier."""
    # Alice resolves via her "Alice" alias
    ra = assessor.assess("u", "whos Alice", link_only=True)
    assert "person/alice-chen" in {x.wiki_id for x in ra.related}


def test_fragment_tier_when_no_alias(assessor):
    """Add an entity with no alias for its first name; a first-name lookup
    must still land on it via the name-fragment tier (no false negatives)."""
    assessor.store.upsert_entity("u", "person", "Anton Lokhy",
                                 summary_append="Famous Instagram account holder.")
    assessor.store.flush()
    r = assessor.assess("u", "whos Anton", link_only=True)
    ids = {x.wiki_id for x in r.related}
    assert "person/anton-lokhy" in ids, f"first-name lookup missed Anton Lokhy: {ids}"
    hit = next(x for x in r.related if x.wiki_id == "person/anton-lokhy")
    assert hit.matched_on == "name-fragment"


def test_fragment_does_not_false_positive_other_names(assessor):
    """A bare first name must not link to an entity whose title merely SHARES
    that token later (e.g. "Orion" must not match a hypothetical Second Orion)
    or to an unrelated entity. "Project Orion" is only matched when the full
    title appears in the question."""
    r = assessor.assess("u", "whos Alice Chen", link_only=True)
    ids = {x.wiki_id for x in r.related}
    assert "person/alice-chen" in ids
    # Orion is not mentioned in the question -> must not appear.
    assert "project/orion" not in ids


# ---------- CJK fuzzy/summary-tier retrieval ----------

def test_chinese_query_fuzzy_matches_a_related_but_differently_worded_entity(assessor):
    """Regression: content_tokens() used to tokenize CJK text one character
    at a time and then filter out anything not longer than 2 characters --
    which silently discarded every CJK token, so Jaccard overlap against
    Chinese text was always 0.0 and the "summary" fuzzy tier never fired.
    Only an exact title/alias substring worked; a shorter, related phrase
    found nothing even though the entity clearly covers it."""
    assessor.store.upsert_entity(
        "u", "organization", "分行整改方案",
        compact="各分支行整改方案", summary_append="各分支行整改方案。")
    assessor.store.flush()

    # The exact phrase from the title still matches (unaffected baseline).
    exact = assessor.assess("u", "分行整改方案", link_only=True)
    assert exact.related, "exact-phrase match must still work"

    # A shorter, related phrase that appears only inside the SUMMARY (not
    # the title) must now also retrieve it via the fuzzy tier.
    fuzzy = assessor.assess("u", "分支行", link_only=True)
    assert fuzzy.related, "fuzzy/summary tier must match related Chinese text"
    hit = fuzzy.related[0]
    assert hit.matched_on == "summary"


# ---------- semantic (embedding) fallback tier ----------

def test_semantic_fallback_matches_a_conceptually_related_entity(assessor, monkeypatch):
    """The tier above all this session's lexical work still can't do:
    "branch" and "department" share no text at all, so no amount of
    tokenizing bridges them -- only a meaning-level comparison can. embed()
    is stubbed to a fixed vector so this proves the tier is reachable and
    wired correctly without needing the real model in a test."""
    import app.graph.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module, "is_embedding_enabled", lambda: True)
    monkeypatch.setattr(embeddings_module, "embed", lambda text: [1.0, 0.0, 0.0])

    assessor.store.upsert_entity(
        "u", "organization", "Branch Rectification Plan",
        compact="Covers branch offices and their compliance workflow.")
    assessor.store.flush()

    r = assessor.assess("u", "what is the departmental structure overview",
                        link_only=True)
    ids = {x.wiki_id for x in r.related}
    assert "organization/branch-rectification-plan" in ids
    hit = next(x for x in r.related
              if x.wiki_id == "organization/branch-rectification-plan")
    assert hit.matched_on == "semantic"


def test_semantic_fallback_is_skipped_once_an_earlier_tier_matches(assessor, monkeypatch):
    """Cost control: the semantic tier must only run on a TOTAL miss across
    every entity, not per-entity -- if anything already matched via title/
    alias/name-fragment/summary, embed() must not be called at all."""
    import app.graph.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module, "is_embedding_enabled", lambda: True)

    def boom(text):
        raise AssertionError("embed() must not be called once an earlier tier matched")

    monkeypatch.setattr(embeddings_module, "embed", boom)

    r = assessor.assess("u", "whos Alice", link_only=True)
    assert "person/alice-chen" in {x.wiki_id for x in r.related}
