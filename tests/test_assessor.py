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


def test_correction_about_known_entity_is_stored(assessor):
    r = assessor.assess("u", "Actually I was wrong earlier - Hybrid Search does not use "
                             "BM25 anymore, it moved to SPLADE in Q2.")
    assert r.decision == "store"
    # Multi-strategy linking: "Hybrid Search" matches via title, "orion"
    # may also match via keyword overlap if the Orion entity has related keywords.
    assert "concept/hybrid-search" in {x.wiki_id for x in r.related}
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
