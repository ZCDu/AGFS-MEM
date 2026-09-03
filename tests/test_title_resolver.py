"""
Tests for the Wiki Title Resolver (app/graph/title_resolver.py, PLAN.md §7.3).
Goes through the HTTP API since that's the actual integration point.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_BUCKET_ROOT", str(tmp_path / "bucket"))
    import app.config as config_module
    import app.deps as deps_module
    config_module._settings = None
    deps_module.get_storage_backend.cache_clear()

    # Context-managed TestClient so FastAPI's lifespan runs backend.close()
    # (drains write-behind buffers, stops the mirage event loop). Without it
    # the backend leaks and its flush timers fire at interpreter shutdown
    # through a dead executor -> a "cannot schedule new futures after
    # shutdown" flood that does not fail tests but buries the summary.
    with TestClient(create_app()) as test_client:
        yield test_client
    deps_module.get_storage_backend.cache_clear()
    shutil.rmtree(tmp_path / "bucket", ignore_errors=True)


def test_resolve_creates_new_entity_when_no_match_and_type_given(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice Chen", "type_hint": "person"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "created"
    assert body["wiki_id"] == "person/alice-chen"
    assert body["entity"]["title"] == "Alice Chen"


def test_resolve_merges_exact_title_match(client):
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice Chen", "type_hint": "person"})

    r = client.post("/v1/users/u1/wiki/resolve", json={
        "title": "Alice Chen", "type_hint": "person", "summary_append": "Works remotely.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "matched"
    assert body["wiki_id"] == "person/alice-chen"
    assert body["entity"]["summary"] == "Works remotely."

    # still only one entity exists
    r = client.get("/v1/users/u1/wiki")
    assert len(r.json()) == 1


def test_resolve_merges_short_name_via_word_containment(client):
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice Chen", "type_hint": "person"})

    # "Alice" alone should match "Alice Chen" via word-containment, not create a duplicate
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice", "type_hint": "person"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "matched"
    assert body["wiki_id"] == "person/alice-chen"
    # "Alice" is now recorded as an alias of the canonical entity
    assert "Alice" in body["entity"]["aliases"]

    r = client.get("/v1/users/u1/wiki")
    assert len(r.json()) == 1


def test_resolve_merges_via_existing_alias(client):
    client.post("/v1/users/u1/wiki/resolve", json={
        "title": "Alice Chen", "type_hint": "person", "aliases": ["AC"],
    })

    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "AC", "type_hint": "person"})
    assert r.status_code == 200
    assert r.json()["action"] == "matched"
    assert r.json()["wiki_id"] == "person/alice-chen"


def test_resolve_without_type_hint_and_no_match_goes_to_inbox(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Something New"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "inbox"
    assert body["inbox_id"] is not None
    assert body["wiki_id"] is None

    # nothing was created in the wiki itself
    assert client.get("/v1/users/u1/wiki").json() == []


def test_resolve_ambiguous_match_across_types_goes_to_inbox(client):
    # "Orion" exists as both a project and a concept — matching without a
    # type_hint can't know which one "Orion" (bare) refers to
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Project Orion", "type_hint": "project"})
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Orion Concept", "type_hint": "concept"})

    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Orion"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "inbox"
    assert "ambiguous" in body["reason"]


def test_inbox_list_and_get(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Mystery Thing"})
    inbox_id = r.json()["inbox_id"]

    r = client.get("/v1/users/u1/wiki/_inbox")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["candidate_id"] == inbox_id

    r = client.get(f"/v1/users/u1/wiki/_inbox/{inbox_id}")
    assert r.status_code == 200
    assert r.json()["title"] == "Mystery Thing"


def test_inbox_get_nonexistent_404(client):
    r = client.get("/v1/users/u1/wiki/_inbox/inbox_does_not_exist")
    assert r.status_code == 404


def test_resolve_inbox_candidate_creates_entity_and_clears_inbox(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Mystery Thing"})
    inbox_id = r.json()["inbox_id"]

    r = client.post(f"/v1/users/u1/wiki/_inbox/{inbox_id}/resolve", json={"type": "concept"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "created"
    assert body["wiki_id"] == "concept/mystery-thing"

    # inbox is now empty
    assert client.get("/v1/users/u1/wiki/_inbox").json() == []
    # and the entity actually exists
    assert client.get("/v1/users/u1/wiki/concept/Mystery Thing").status_code == 200


def test_resolve_inbox_candidate_can_merge_instead_of_create(client):
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice Chen", "type_hint": "person"})
    client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice Corp", "type_hint": "organization"})

    # "Alice" alone word-contains BOTH existing entities -> genuinely
    # ambiguous without a type_hint -> inbox (unlike the single-match case
    # in test_resolve_merges_short_name_via_word_containment)
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Alice"})
    assert r.json()["action"] == "inbox"
    inbox_id = r.json()["inbox_id"]

    # manually resolving it as type=person narrows the search to just
    # person-type entities, which now has exactly one match -> merges
    # into the existing Alice Chen instead of creating a new one
    r = client.post(f"/v1/users/u1/wiki/_inbox/{inbox_id}/resolve", json={"type": "person"})
    assert r.status_code == 200
    assert r.json()["action"] == "matched"
    assert r.json()["wiki_id"] == "person/alice-chen"

    r = client.get("/v1/users/u1/wiki")
    assert len(r.json()) == 2  # Alice Chen + Alice Corp, no new duplicate created


def test_resolve_inbox_candidate_with_title_override(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Mystery Thing"})
    inbox_id = r.json()["inbox_id"]

    r = client.post(f"/v1/users/u1/wiki/_inbox/{inbox_id}/resolve", json={
        "type": "concept", "title": "Renamed Concept",
    })
    assert r.status_code == 200
    assert r.json()["wiki_id"] == "concept/renamed-concept"


def test_discard_inbox_candidate(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "Junk"})
    inbox_id = r.json()["inbox_id"]

    r = client.delete(f"/v1/users/u1/wiki/_inbox/{inbox_id}")
    assert r.status_code == 204
    assert client.get("/v1/users/u1/wiki/_inbox").json() == []

    r = client.delete(f"/v1/users/u1/wiki/_inbox/{inbox_id}")
    assert r.status_code == 404


def test_resolve_invalid_type_hint_rejected(client):
    r = client.post("/v1/users/u1/wiki/resolve", json={"title": "X", "type_hint": "not_a_type"})
    assert r.status_code == 422
