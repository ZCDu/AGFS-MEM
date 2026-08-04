"""
Slug collision tests.

wiki_id is derived from the title by discarding every non-alphanumeric
character, so distinct titles can collide. Previously the second write simply
merged into the first and discarded its title — silent data loss.

The hard part is that two DIFFERENT situations produce the same slug:

    "Alice Chen" / "ALICE chen!"  -> same entity, typed differently. Merge.
    "C++"        / "C#"           -> different concepts. Must not merge.

title_resolver._normalize cannot tell them apart, because it strips
punctuation and maps both "C++" and "C#" to "c". _title_key preserves internal
punctuation and strips only the ends, which separates them.
"""

from __future__ import annotations

import tempfile

import pytest

from app.graph.store import (EntityGraphStore, SlugConflictError, _slugify,
                             _title_key, has_usable_slug)
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend)
    backend.close()


# ---------- the discriminator ----------

def test_title_key_separates_meaningful_punctuation_from_noise():
    # Trailing punctuation, case and spacing are noise.
    assert _title_key("Alice Chen") == _title_key("ALICE  chen!")
    assert _title_key(".NET") == _title_key("NET")
    # Internal punctuation is meaning.
    assert _title_key("C++") != _title_key("C#")
    assert _title_key("R&D") != _title_key("R and D")


def test_normalize_cannot_be_used_for_this():
    """Documents why a separate helper exists: the resolver's normaliser maps
    two different languages to the same string."""
    from app.graph.title_resolver import _normalize
    assert _normalize("C++") == _normalize("C#") == "c"
    assert _slugify("C++") == _slugify("C#") == "c"


def test_titles_with_no_alphanumerics_have_no_usable_slug():
    for bad in ("!!!", "???", "...", "   ", "---"):
        assert not has_usable_slug(bad)
    for good in ("Alice", "C++", "3M", "x"):
        assert has_usable_slug(good)


# ---------- same entity, typed differently ----------

def test_variant_spelling_merges_and_keeps_the_variant_as_an_alias(store):
    """Merging is right here. Throwing the variant away was the silent part —
    it means the other spelling never resolves later."""
    store.upsert_entity("u", "person", "Alice Chen", summary_append="first")
    result = store.upsert_entity("u", "person", "ALICE  chen!", summary_append="second")

    assert result.wiki_id == "person/alice-chen"
    assert result.title == "Alice Chen", "the canonical title should not be overwritten"
    assert "ALICE  chen!" in result.aliases
    assert store.stats("u")["entities"] == 1
    assert "first" in result.summary and "second" in result.summary


def test_exact_same_title_is_a_plain_update_with_no_alias_noise(store):
    store.upsert_entity("u", "person", "Alice Chen")
    result = store.upsert_entity("u", "person", "Alice Chen", summary_append="more")
    assert result.aliases == []


def test_variant_is_not_added_twice(store):
    store.upsert_entity("u", "person", "Alice Chen")
    store.upsert_entity("u", "person", "alice chen")
    result = store.upsert_entity("u", "person", "alice chen")
    assert result.aliases.count("alice chen") == 1


# ---------- genuinely different titles ----------

def test_colliding_distinct_titles_are_refused(store):
    store.upsert_entity("u", "concept", "C++", summary_append="the language")

    with pytest.raises(SlugConflictError) as exc:
        store.upsert_entity("u", "concept", "C#")

    e = exc.value
    assert e.wiki_id == "concept/c"
    assert e.existing_title == "C++"
    assert e.incoming_title == "C#"
    assert e.suggestion == "concept/c-2", "the error must offer a usable alternative"

    # And the original is untouched.
    assert store.get_entity("u", "concept/c", touch=False).title == "C++"
    assert "the language" in store.get_entity("u", "concept/c", touch=False).summary


def test_disambiguate_keeps_both(store):
    store.upsert_entity("u", "concept", "C++")
    second = store.upsert_entity("u", "concept", "C#", on_conflict="disambiguate")

    assert second.wiki_id == "concept/c-2"
    assert second.title == "C#"
    assert {"concept/c", "concept/c-2"} <= set(store.list_entities("u"))

    third = store.upsert_entity("u", "concept", "C--", on_conflict="disambiguate")
    assert third.wiki_id == "concept/c-3", "suffixes must keep climbing"


def test_merge_is_still_available_but_must_be_explicit(store):
    """The old implicit behaviour, now opt-in.

    The incoming title is DISCARDED and the existing entity keeps its own —
    which is exactly the silent data loss this work was about. Available for
    callers who genuinely want last-write-wins into one entity, but they now
    have to say so.
    """
    store.upsert_entity("u", "concept", "C++")
    merged = store.upsert_entity("u", "concept", "C#", on_conflict="merge",
                                 summary_append="csharp notes")
    assert merged.wiki_id == "concept/c"
    assert merged.title == "C++", "merge keeps the existing title; the new one is lost"
    assert "csharp notes" in merged.summary, "but the content is merged in"
    assert store.stats("u")["entities"] == 1


def test_unnameable_titles_are_rejected(store):
    """_slugify falls back to the literal "entity", so every unnameable title
    would collide with every other one."""
    with pytest.raises(ValueError, match="no letters or digits"):
        store.upsert_entity("u", "concept", "!!!")
    with pytest.raises(ValueError, match="no letters or digits"):
        store.upsert_entity("u", "concept", "...")


def test_bad_on_conflict_value_is_rejected(store):
    with pytest.raises(ValueError, match="on_conflict"):
        store.upsert_entity("u", "person", "Someone", on_conflict="whatever")


def test_a_tombstoned_slug_can_be_reused(store):
    """A deleted entity should not block the name forever."""
    store.upsert_entity("u", "concept", "C++")
    store.delete_entity("u", "concept/c", hard_delete=False)
    revived = store.upsert_entity("u", "concept", "C#")
    assert revived.wiki_id == "concept/c"


# ---------- over the API ----------

def test_api_returns_409_with_a_usable_alternative(client):
    u = "/v1/users/demo/wiki"
    client.put(u, json={"type": "concept", "title": "C++"})

    r = client.put(u, json={"type": "concept", "title": "C#"})
    assert r.status_code == 409
    body = r.json()
    assert body["existing_title"] == "C++"
    assert body["incoming_title"] == "C#"
    assert body["suggested_wiki_id"] == "concept/c-2"

    ok = client.put(u, json={"type": "concept", "title": "C#",
                             "on_conflict": "disambiguate"})
    assert ok.status_code == 200
    assert ok.json()["wiki_id"] == "concept/c-2"


def test_api_rejects_unnameable_title_with_422_not_500(client):
    r = client.put("/v1/users/demo/wiki", json={"type": "concept", "title": "!!!"})
    assert r.status_code == 422
    assert "no letters or digits" in r.json()["detail"]
