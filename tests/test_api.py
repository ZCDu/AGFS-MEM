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

    app = create_app()
    yield TestClient(app)
    shutil.rmtree(tmp_path / "bucket", ignore_errors=True)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_entity_upsert_and_get(client):
    r = client.put("/v1/users/u1/wiki", json={
        "type": "person", "title": "Alice Chen", "summary_append": "Engineer.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Alice Chen"
    assert body["type"] == "person"
    assert body["wiki_id"] == "person/alice-chen"
    assert body["summary"] == "Engineer."
    assert body["compact"] == "Engineer."  # auto-derived stopgap
    # "1.0" was never a real OKF version; the spec is at 0.1.
    assert body["okf_version"] == "0.1"
    assert body["metadata"]["user_id"] == "u1"

    r = client.get("/v1/users/u1/wiki/person/Alice Chen")
    assert r.status_code == 200
    assert r.json()["wiki_id"] == "person/alice-chen"


def test_unknown_type_rejected(client):
    r = client.put("/v1/users/u1/wiki", json={"type": "not_a_type", "title": "X"})
    assert r.status_code == 422


def test_entity_not_found(client):
    r = client.get("/v1/users/u1/wiki/person/Nobody")
    assert r.status_code == 404


def test_add_fact(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    r = client.post("/v1/users/u1/wiki/person/Alice/facts", json={
        "text": "Works on Project Orion.", "confidence": 0.9, "evidence": ["journals/x.jsonl#L1"],
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["facts"]) == 1
    assert body["facts"][0]["fact_id"] == "fact_0001"
    assert body["facts"][0]["confidence"] == 0.9


def test_update_fact(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.post("/v1/users/u1/wiki/person/Alice/facts", json={"text": "Original text.", "confidence": 0.5})

    r = client.patch("/v1/users/u1/wiki/person/Alice/facts/fact_0001", json={
        "text": "Corrected text.", "confidence": 0.99,
    })
    assert r.status_code == 200
    fact = r.json()["facts"][0]
    assert fact["text"] == "Corrected text."
    assert fact["confidence"] == 0.99


def test_update_nonexistent_fact_404(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    r = client.patch("/v1/users/u1/wiki/person/Alice/facts/fact_9999", json={"text": "x"})
    assert r.status_code == 404


def test_remove_fact(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.post("/v1/users/u1/wiki/person/Alice/facts", json={"text": "Fact one."})
    client.post("/v1/users/u1/wiki/person/Alice/facts", json={"text": "Fact two."})

    r = client.delete("/v1/users/u1/wiki/person/Alice/facts/fact_0001")
    assert r.status_code == 200
    remaining = [f["fact_id"] for f in r.json()["facts"]]
    assert remaining == ["fact_0002"]

    r = client.delete("/v1/users/u1/wiki/person/Alice/facts/fact_0001")
    assert r.status_code == 404


def test_update_relation(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to", "label": "works_on", "weight": 0.5,
    })

    r = client.patch("/v1/users/u1/wiki/person/Alice/relations/rel_0001", json={
        "label": "leads", "weight": 1.0, "reason": "promoted to lead",
    })
    assert r.status_code == 200
    rel = r.json()["relations"][0]
    assert rel["label"] == "leads"
    assert rel["weight"] == 1.0
    assert rel["reason"] == "promoted to lead"


def test_remove_relation(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    r = client.delete("/v1/users/u1/wiki/person/Alice/relations/rel_0001")
    assert r.status_code == 200
    assert r.json()["relations"] == []

    r = client.delete("/v1/users/u1/wiki/person/Alice/relations/rel_0001")
    assert r.status_code == 404


def test_delete_cascades_dangling_relations(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    r = client.get("/v1/users/u1/wiki/person/Alice/relations")
    assert len(r.json()) == 1

    client.delete("/v1/users/u1/wiki/project/Orion")

    # cascade should have stripped the dangling relation from Alice
    r = client.get("/v1/users/u1/wiki/person/Alice/relations")
    assert r.json() == []

    # and traverse() should never report a wiki_id that doesn't resolve
    r = client.post("/v1/users/u1/wiki/traverse", json={
        "entry_wiki_ids": ["person/alice"], "max_depth": 2,
    })
    assert r.json()["wiki_ids"] == ["person/alice"]


def test_delete_cascade_false_leaves_dangling_relation(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    client.delete("/v1/users/u1/wiki/project/Orion", params={"cascade": "false"})

    r = client.get("/v1/users/u1/wiki/person/Alice/relations")
    assert len(r.json()) == 1  # cascade opted out — dangling relation remains


def test_reconcile_sweep_catches_dangling_relations(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    # simulate the race the cascade is meant to prevent: delete WITHOUT cascade,
    # so Alice is left holding a dangling relation
    client.delete("/v1/users/u1/wiki/project/Orion", params={"cascade": "false"})
    assert len(client.get("/v1/users/u1/wiki/person/Alice/relations").json()) == 1

    r = client.post("/v1/users/u1/wiki/_reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body["entities_fixed"] == 1
    assert body["relations_removed"] == 1

    assert client.get("/v1/users/u1/wiki/person/Alice/relations").json() == []


def test_soft_delete_leaves_tombstone_when_hard_delete_false(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})

    r = client.delete("/v1/users/u1/wiki/person/Alice", params={"hard_delete": "false"})
    assert r.status_code == 204

    # invisible to normal reads...
    assert client.get("/v1/users/u1/wiki/person/Alice").status_code == 404
    assert "person/alice" not in {
        e["wiki_id"] for e in client.get("/v1/users/u1/wiki").json()
    }

    # ...but still there as a tombstone if you explicitly ask
    r = client.get("/v1/users/u1/wiki/person/Alice", params={"include_deleted": "true"})
    assert r.status_code == 200
    assert r.json()["status"] == "deprecated"


def test_soft_deleted_entity_never_resolves_via_traverse(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    # tombstone Orion without cascading and without hard-deleting — the
    # dangling relation from Alice is still physically present in storage
    client.delete("/v1/users/u1/wiki/project/Orion",
                   params={"cascade": "false", "hard_delete": "false"})

    r = client.post("/v1/users/u1/wiki/traverse", json={
        "entry_wiki_ids": ["person/alice"], "max_depth": 2,
    })
    # Orion must never appear as "reached", even though its file still
    # exists on disk — this is the whole point of checking status, not
    # just file existence, at every read path.
    assert r.json()["wiki_ids"] == ["person/alice"]


def test_upsert_revives_a_tombstone(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.delete("/v1/users/u1/wiki/person/Alice", params={"hard_delete": "false"})
    assert client.get("/v1/users/u1/wiki/person/Alice").status_code == 404

    # re-upserting the same (type, title) should revive it, not error
    r = client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    assert r.status_code == 200
    assert r.json()["status"] == "stable"

    r = client.get("/v1/users/u1/wiki/person/Alice")
    assert r.status_code == 200


def test_double_delete_still_404s_with_default_hard_delete(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    r = client.delete("/v1/users/u1/wiki/person/Alice")
    assert r.status_code == 204
    r = client.delete("/v1/users/u1/wiki/person/Alice")
    assert r.status_code == 404


def test_reconcile_sweep_is_idempotent_noop_when_clean(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })

    r = client.post("/v1/users/u1/wiki/_reconcile")
    assert r.json() == {
        "entities_scanned": 2, "entities_fixed": 0,
        "relations_removed": 0, "entities_failed": [],
    }
    assert len(client.get("/v1/users/u1/wiki/person/Alice/relations").json()) == 1


def test_remove_relation_bidirectional(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to", "bidirectional": True,
    })

    assert len(client.get("/v1/users/u1/wiki/person/Alice/relations").json()) == 1
    assert len(client.get("/v1/users/u1/wiki/project/Orion/relations").json()) == 1

    client.delete(
        "/v1/users/u1/wiki/person/Alice/relations/rel_0001", params={"bidirectional": "true"}
    )

    assert client.get("/v1/users/u1/wiki/person/Alice/relations").json() == []
    assert client.get("/v1/users/u1/wiki/project/Orion/relations").json() == []


def test_link_and_traverse(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.put("/v1/users/u1/wiki", json={"type": "artifact", "title": "AWS"})

    r = client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to", "label": "works_on",
    })
    assert r.status_code == 200
    r = client.post("/v1/users/u1/wiki/project/Orion/relations", json={
        "target_wiki_id": "artifact/aws", "category": "related_to", "label": "hosted_on",
    })
    assert r.status_code == 200

    r = client.post("/v1/users/u1/wiki/traverse", json={
        "entry_wiki_ids": ["person/alice"], "max_depth": 2,
    })
    assert r.status_code == 200
    assert set(r.json()["wiki_ids"]) == {"person/alice", "project/orion", "artifact/aws"}

    r = client.post("/v1/users/u1/wiki/traverse", json={
        "entry_wiki_ids": ["person/alice"], "max_depth": 1,
    })
    assert set(r.json()["wiki_ids"]) == {"person/alice", "project/orion"}


def test_invalid_relation_category_rejected(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    r = client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "not_a_real_category",
    })
    assert r.status_code == 422


def test_list_entities_from_manifest(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})

    r = client.get("/v1/users/u1/wiki")
    assert r.status_code == 200
    wiki_ids = {e["wiki_id"] for e in r.json()}
    assert wiki_ids == {"person/alice", "project/orion"}

    r = client.get("/v1/users/u1/wiki", params={"type": "person"})
    assert r.status_code == 200
    assert {e["wiki_id"] for e in r.json()} == {"person/alice"}


def test_delete_entity(client):
    client.put("/v1/users/u1/wiki", json={"type": "concept", "title": "Temp"})
    r = client.delete("/v1/users/u1/wiki/concept/Temp")
    assert r.status_code == 204
    r = client.delete("/v1/users/u1/wiki/concept/Temp")
    assert r.status_code == 404

    # deleting removes it from the manifest too
    r = client.get("/v1/users/u1/wiki")
    assert "concept/temp" not in {e["wiki_id"] for e in r.json()}


def test_graph_stats(client):
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    client.put("/v1/users/u1/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/u1/wiki/person/Alice/relations", json={
        "target_wiki_id": "project/orion", "category": "related_to",
    })
    r = client.get("/v1/users/u1/wiki/_stats")
    assert r.status_code == 200
    assert r.json() == {"entities": 2, "edges": 1}


def test_raw_facts_append_and_read(client):
    r = client.post("/v1/users/u1/raw-facts", json={"facts": [{"fact": "hello"}, {"fact": "world"}]})
    assert r.status_code == 201
    assert r.json()["count"] == 2

    from datetime import date
    r = client.get("/v1/users/u1/raw-facts", params={"on": date.today().isoformat()})
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_wiki_scoped_by_user_id_not_team(client):
    """Two different user_ids never collide — this is the isolation
    boundary now that team_id has been removed (single-tenant per user,
    matching the design doc's <user_id>/wiki/... layout)."""
    client.put("/v1/users/u1/wiki", json={"type": "person", "title": "Alice"})
    r = client.get("/v1/users/u2/wiki/person/Alice")
    assert r.status_code == 404
    r = client.get("/v1/users/u1/wiki/person/Alice")
    assert r.status_code == 200


def test_gui_is_served_from_the_apps_own_origin(client):
    """The editor must be served BY the app, not opened from disk. There is no
    CORS middleware, so a file:// page could not call the API at all."""
    r = client.get("/gui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Memory graph" in r.text


def test_entity_list_exposes_edges_for_graph_drawing(client):
    """The GUI draws the whole graph from one request, which only works if the
    list response carries the adjacency index."""
    client.put("/v1/users/g/wiki", json={"type": "person", "title": "A"})
    client.put("/v1/users/g/wiki", json={"type": "project", "title": "B"})
    client.post("/v1/users/g/wiki/person/a/relations",
                json={"target_wiki_id": "project/b", "label": "leads"})

    rows = client.get("/v1/users/g/wiki").json()
    by_id = {r["wiki_id"]: r for r in rows}
    assert by_id["person/a"]["edges"] == [{"t": "project/b", "c": "related_to"}]
    assert by_id["project/b"]["edges"] == []


def test_gui_overlays_cannot_swallow_canvas_clicks(client):
    """Regression guard for a CSS specificity bug that made the graph appear
    completely dead.

    The empty-state overlay is `position: absolute; inset: 0`, so it covers the
    whole canvas. `[hidden]` is only a USER-AGENT rule, and the author-level
    `.empty { display: grid }` overrode it — so setting .hidden from JS had no
    effect, the overlay stayed on screen after entities were added, and it
    intercepted every click aimed at a node.

    Two things must hold: overlays never take pointer events, and [hidden] is
    re-asserted at author level.
    """
    css = client.get("/gui").text
    assert ".empty[hidden] { display: none; }" in css, \
        "author-level [hidden] override missing; overlay will never hide"
    assert css.count("pointer-events: none") >= 2, \
        "canvas overlays must not intercept clicks"


def test_no_hidden_element_sets_display_in_an_inline_style(client):
    """Inline styles outrank the user-agent `[hidden] { display: none }` rule,
    so an element with both is permanently visible however the JS sets
    .hidden. Cost me twice: the empty-state overlay, then the sign-in modal —
    which as a full-screen z-index:20 overlay swallowed every click meant for
    the header, including the button that opens it.

    Every element controlled by `hidden` must get its display from CSS, where
    an author-level `[hidden]` rule can win.
    """
    import re
    html = client.get("/gui").text
    offenders = re.findall(r"<[^>]*\bhidden\b[^>]*display\s*:[^>]*>", html)
    assert not offenders, f"element(s) with both `hidden` and inline display: {offenders}"


def _gui_script(client) -> str:
    import re
    html = client.get("/gui").text
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)


def test_gui_script_has_no_duplicate_top_level_declarations(client):
    """A duplicated `let` is a fatal SyntaxError, and a SyntaxError anywhere in
    the script means NOTHING in it runs — no handlers attached, every button
    dead, no error visible on the page.

    This shipped: two overlapping edits each added a `let saveTimer`, and the
    entire GUI silently did nothing. Cheap to detect, catastrophic to miss.
    """
    import re
    js = _gui_script(client)
    seen: dict[str, int] = {}
    for line in js.split("\n"):
        m = re.match(r"(let|const)\s+([A-Za-z_$][\w$]*)\s*=", line)
        if m:  # column 0 only, so nested scopes are ignored
            seen[m.group(2)] = seen.get(m.group(2), 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, f"duplicate top-level declarations (fatal SyntaxError): {dupes}"


def test_gui_script_parses(client):
    """Parse the script with a real JS engine when one is available.

    A hand-rolled brace counter cannot do this: comments, regex literals and
    template literals all contain unbalanced braces, and a naive count reports
    false failures. Node is the correct tool; the test skips where it is
    absent rather than shipping an unreliable approximation.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; duplicate-declaration test still covers "
                    "the failure mode that actually shipped")

    js = _gui_script(client)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        path = f.name
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, f"GUI script does not parse:\n{result.stderr}"


def test_every_gui_element_reference_resolves(client):
    """$("id") on an element that does not exist returns null, and the next
    property access throws — killing every handler registered after it."""
    import re
    html = client.get("/gui").text
    defined = {i for i in re.findall(r'id="([^"]+)"', html) if "${" not in i}
    referenced = set(re.findall(r'\$\("([^"]+)"\)', html))
    missing = referenced - defined
    assert not missing, f"$() references non-existent elements: {sorted(missing)}"


def test_entity_files_conform_to_okf(client):
    """OKF v0.1 §9: every non-reserved .md file must have parseable YAML
    frontmatter containing a non-empty `type`. Those are the only hard
    requirements; everything else in the spec is guidance consumers must
    tolerate the absence of.
    """
    import re

    import yaml

    client.put("/v1/users/demo/wiki", json={
        "type": "person", "title": "Alice Chen", "aliases": ["Alice"],
        "summary_append": "Staff engineer on the retrieval team."})
    client.put("/v1/users/demo/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/demo/wiki/person/alice-chen/facts",
                json={"text": "Leads retrieval.", "evidence": ["session:2026-08-03:x"]})
    client.post("/v1/users/demo/wiki/person/alice-chen/relations",
                json={"target_wiki_id": "project/orion", "label": "leads"})

    import app.deps as deps
    backend = deps.get_storage_backend()
    keys = [k for k in backend.list_keys("demo/wiki/") if k.endswith(".md")]
    assert keys

    for key in keys:
        text = backend.get_bytes(key).data.decode("utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        assert m, f"{key} has no frontmatter block"
        front = yaml.safe_load(m.group(1))
        assert front.get("type"), f"{key} has no non-empty `type`"
        # Recommended fields, in OKF's spelling rather than only ours.
        assert "title" in front and "description" in front and "timestamp" in front


def test_relations_appear_as_markdown_links_in_the_body(client):
    """OKF §5: relationships are ordinary markdown links in the BODY, and §5.3
    says a consumer building a graph treats links as edges. A graph held only
    in frontmatter is invisible to every generic OKF consumer, which is most
    of the reason to adopt the format."""
    client.put("/v1/users/demo/wiki", json={"type": "person", "title": "Alice Chen"})
    client.put("/v1/users/demo/wiki", json={"type": "project", "title": "Orion"})
    client.post("/v1/users/demo/wiki/person/alice-chen/relations",
                json={"target_wiki_id": "project/orion", "label": "leads"})

    import app.deps as deps
    text = deps.get_storage_backend().get_bytes(
        "demo/wiki/person/alice-chen.md").data.decode("utf-8")

    body = text.split("---\n", 2)[2]
    assert "[project/orion](/project/orion.md)" in body, "bundle-relative link per §5.1"
    assert "## Relations" in body


def test_generated_body_sections_do_not_accumulate(client):
    """The body is free-form markdown; facts/relations live in the .json sidecar.
    Repeated writes must not duplicate facts or relations in the body."""
    client.put("/v1/users/demo/wiki", json={
        "type": "person", "title": "Alice Chen",
        "summary_append": "Staff engineer."})
    client.post("/v1/users/demo/wiki/person/alice-chen/facts",
                json={"text": "Leads retrieval."})

    for _ in range(3):
        client.put("/v1/users/demo/wiki", json={"type": "person", "title": "Alice Chen"})

    entity = client.get("/v1/users/demo/wiki/person/alice-chen").json()
    assert entity["summary"] == "Staff engineer."
    assert "## Facts" not in entity["summary"]


def test_frontmatter_does_not_duplicate_okf_fields(client):
    """`aliases` and `compact` said exactly what `tags` and `description` say.
    Carrying both made the frontmatter read as a database dump rather than the
    index card OKF §4.1 describes."""
    import re

    import yaml

    client.put("/v1/users/demo/wiki", json={
        "type": "person", "title": "Alexei", "aliases": ["Alexei Krasny"],
        "summary_append": "A trusted comrade."})

    import app.deps as deps
    text = deps.get_storage_backend().get_bytes(
        "demo/wiki/person/alexei.md").data.decode("utf-8")
    front = yaml.safe_load(re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL).group(1))

    assert front["tags"] == ["Alexei Krasny"]
    assert front["description"] == "A trusted comrade."
    assert "aliases" not in front and "compact" not in front


def test_old_files_using_aliases_and_compact_still_load(client):
    """Dropping fields from the writer must not orphan files already written."""
    import app.deps as deps
    backend = deps.get_storage_backend()
    backend.put_bytes("demo/wiki/person/legacy.md", b"""---
type: person
title: Legacy Person
wiki_id: person/legacy
aliases:
- Old Alias
compact: Written before the field rename.
facts: []
relations: []
status: active
merged_into: null
metadata:
  significance: 0.5
  last_accessed: '2026-01-01T00:00:00+00:00'
  created_at: '2026-01-01T00:00:00+00:00'
  updated_at: '2026-01-01T00:00:00+00:00'
  user_id: demo
---

# Legacy Person

Written before the field rename.
""")
    got = client.get("/v1/users/demo/wiki/person/legacy").json()
    assert got["aliases"] == ["Old Alias"]
    assert got["compact"] == "Written before the field rename."


def test_body_carries_the_detail_a_reader_needs(client):
    """A body that only repeats `description` is not worth reading. §4.2 asks
    producers to favour structural markdown, and confidence is meaningless to
    a reader if it lives only in frontmatter they were not meant to read."""
    client.put("/v1/users/demo/wiki", json={"type": "person", "title": "Alexei",
                                            "summary_append": "A trusted comrade."})
    client.put("/v1/users/demo/wiki", json={"type": "person", "title": "Yin"})
    client.post("/v1/users/demo/wiki/person/alexei/facts",
                json={"text": "Fought in the war.", "confidence": 0.8,
                      "evidence": ["session:2026-08-03:x"]})
    client.post("/v1/users/demo/wiki/person/alexei/relations",
                json={"target_wiki_id": "person/yin", "label": "comrade_of",
                      "reason": "Fought together."})

    import app.deps as deps
    text = deps.get_storage_backend().get_bytes(
        "demo/wiki/person/alexei.md").data.decode("utf-8")
    body = text.split("---\n", 2)[2]

    assert "| Fact | Confidence | Source |" in body, "facts belong in a table"
    assert "| Fought in the war. | 0.8 | session:2026-08-03:x |" in body
    assert "**comrade of** [person/yin](/person/yin.md) — Fought together." in body
    assert "## Citations" in body
