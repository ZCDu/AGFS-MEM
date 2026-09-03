"""
Subgraph and layout tests.

The invariant that makes a subgraph useful is CLOSURE: every returned edge has
both endpoints in the returned node set. Paging a graph by index does not have
that property — you get edges pointing at nodes you were not sent, which cannot
be laid out and cannot be told apart from genuinely dangling references.
"""

from __future__ import annotations

import random
import tempfile

import pytest

from app.graph.store import EntityGraphStore
from app.storage.mirage_backend import MirageBackend


@pytest.fixture()
def store():
    backend = MirageBackend.from_disk(root=tempfile.mkdtemp())
    yield EntityGraphStore(backend)
    backend.close()


def chain(store, n=6):
    """a -> b -> c -> ... so depth is easy to reason about."""
    for i in range(n):
        store.upsert_entity("u", "concept", f"N{i}", summary_append="s")
    for i in range(n - 1):
        store.link_entities("u", f"concept/n{i}", f"concept/n{i+1}")
    store.flush()


# ---------- the closure invariant ----------

def test_every_returned_edge_has_both_endpoints_present(store):
    random.seed(7)
    n = 80
    for i in range(n):
        store.upsert_entity("u", "concept", f"C{i}")
    for i in range(n):
        for _ in range(3):
            j = random.randrange(n)
            if i != j:
                try:
                    store.link_entities("u", f"concept/c{i}", f"concept/c{j}")
                except Exception:
                    pass
    store.flush()

    for depth in (0, 1, 2, 3):
        g = store.subgraph("u", ["concept/c0"], max_depth=depth, max_nodes=200)
        ids = {node["wiki_id"] for node in g["nodes"]}
        dangling = [e for e in g["edges"]
                    if e["source"] not in ids or e["target"] not in ids]
        assert not dangling, f"depth {depth} returned {len(dangling)} unusable edges"


def test_depth_controls_reach(store):
    chain(store, 6)
    sizes = [len(store.subgraph("u", ["concept/n0"], max_depth=d,
                                max_nodes=100)["nodes"]) for d in (0, 1, 2, 3)]
    assert sizes == [1, 2, 3, 4]


def test_expansion_follows_edges_in_both_directions(store):
    """Expanding a node must reach what points AT it too, or half the graph is
    invisible depending on which end you started from."""
    chain(store, 3)
    g = store.subgraph("u", ["concept/n2"], max_depth=1, max_nodes=50)
    assert {n["wiki_id"] for n in g["nodes"]} == {"concept/n2", "concept/n1"}


def test_hidden_neighbours_counts_what_was_left_out(store):
    """This is what lets a UI show an expand affordance instead of implying a
    node is a leaf."""
    for i in range(5):
        store.upsert_entity("u", "concept", f"Leaf {i}")
    store.upsert_entity("u", "person", "Hub")
    for i in range(5):
        store.link_entities("u", "person/hub", f"concept/leaf-{i}")
    store.flush()

    g = store.subgraph("u", ["person/hub"], max_depth=0, max_nodes=50)
    hub = g["nodes"][0]
    assert hub["degree"] == 5
    assert hub["hidden_neighbours"] == 5, "all five neighbours were excluded"

    g2 = store.subgraph("u", ["person/hub"], max_depth=1, max_nodes=50)
    hub2 = next(n for n in g2["nodes"] if n["wiki_id"] == "person/hub")
    assert hub2["hidden_neighbours"] == 0, "now they are all present"


def test_max_nodes_caps_and_reports_truncation(store):
    chain(store, 20)
    g = store.subgraph("u", ["concept/n0"], max_depth=19, max_nodes=5)
    assert len(g["nodes"]) == 5
    assert g["truncated"] is True
    assert g["total_entities"] == 20


def test_seeds_from_hubs_when_no_entry_given(store):
    """The highest-degree nodes are the useful way into an unfamiliar graph."""
    for i in range(10):
        store.upsert_entity("u", "concept", f"Leaf {i}")
    store.upsert_entity("u", "person", "Hub")
    for i in range(10):
        store.link_entities("u", "person/hub", f"concept/leaf-{i}")
    store.flush()

    g = store.subgraph("u", None, max_depth=0, max_nodes=5)
    # `seeds` reports what the walk started from. The `nodes` array is sorted
    # by wiki_id for a stable response, so asserting on nodes[0] tests
    # alphabetical order rather than seed selection.
    assert g["seeds"][0] == "person/hub"
    assert "person/hub" in {n["wiki_id"] for n in g["nodes"]}


# ---------- connected components ----------

def test_nodes_in_different_clusters_report_different_components(store):
    chain(store, 3)  # n0 -> n1 -> n2, one cluster
    store.upsert_entity("u", "concept", "X")
    store.upsert_entity("u", "concept", "Y")
    store.link_entities("u", "concept/x", "concept/y")  # a second, disjoint cluster
    store.flush()

    g = store.subgraph("u", None, max_depth=3, max_nodes=50)
    by_id = {n["wiki_id"]: n["component"] for n in g["nodes"]}
    assert by_id["concept/n0"] == by_id["concept/n1"] == by_id["concept/n2"]
    assert by_id["concept/x"] == by_id["concept/y"]
    assert by_id["concept/n0"] != by_id["concept/x"]


def test_fully_connected_wiki_has_one_component(store):
    chain(store, 4)
    g = store.subgraph("u", None, max_depth=3, max_nodes=50)
    components = {n["component"] for n in g["nodes"]}
    assert components == {0}


def test_default_seeds_backfill_a_small_disconnected_cluster(store):
    """Regression: the top-3-by-degree default seeds have no component
    awareness, so a small disconnected cluster could lose every tiebreak
    against a large one and be completely absent from the response --
    not just visually unseparated, never rendered at all (this is the
    exact path a first page load in the GUI always takes)."""
    # A large, densely-linked cluster whose members dominate by degree.
    for i in range(6):
        store.upsert_entity("u", "concept", f"Big {i}")
    for i in range(6):
        for j in range(i + 1, 6):
            store.link_entities("u", f"concept/big-{i}", f"concept/big-{j}")
    # A small, totally disconnected cluster.
    store.upsert_entity("u", "concept", "Small A")
    store.upsert_entity("u", "concept", "Small B")
    store.link_entities("u", "concept/small-a", "concept/small-b")
    store.flush()

    g = store.subgraph("u", None, max_depth=1, max_nodes=50)
    ids = {n["wiki_id"] for n in g["nodes"]}
    assert "concept/small-a" in ids or "concept/small-b" in ids, \
        "the small disconnected cluster must not be entirely absent"


def test_default_seeds_unchanged_for_a_single_component_wiki(store):
    """The backfill must ADD seeds only when other components exist and
    aren't covered -- a single-component wiki's seed selection must be
    byte-identical to the plain top-3-by-degree ranking."""
    for i in range(6):
        store.upsert_entity("u", "concept", f"C{i}")
    # A single connected component: C0 is the hub.
    for i in range(1, 6):
        store.link_entities("u", "concept/c0", f"concept/c{i}")
    store.flush()

    entries = {e.wiki_id: e for e in store.manifest.list_entries("u")}
    from app.graph.components import build_adjacency
    adj = build_adjacency(list(entries.values()))
    expected = sorted(entries, key=lambda w: (-len(adj[w]), w))[:3]

    g = store.subgraph("u", None, max_depth=0, max_nodes=50)
    assert g["seeds"] == expected


def test_category_filter_restricts_traversal(store):
    store.upsert_entity("u", "concept", "A")
    store.upsert_entity("u", "concept", "B")
    store.upsert_entity("u", "concept", "C")
    store.link_entities("u", "concept/a", "concept/b", category="refines")
    store.link_entities("u", "concept/a", "concept/c", category="contradicts")
    store.flush()

    g = store.subgraph("u", ["concept/a"], max_depth=1, max_nodes=50,
                       categories={"refines"})
    assert {n["wiki_id"] for n in g["nodes"]} == {"concept/a", "concept/b"}


def test_deleted_entities_are_excluded(store):
    chain(store, 3)
    store.delete_entity("u", "concept/n1", hard_delete=False)
    store.flush()
    g = store.subgraph("u", ["concept/n0"], max_depth=3, max_nodes=50)
    ids = {n["wiki_id"] for n in g["nodes"]}
    assert "concept/n1" not in ids
    assert "concept/n2" not in ids, "the path ran through the deleted node"


# ---------- persisted layout ----------

def test_positions_persist_and_are_returned(store):
    chain(store, 2)
    assert store.manifest.set_positions("u", {"concept/n0": (42.0, -13.5)}) == 1
    store.flush()

    g = store.subgraph("u", ["concept/n0"], max_depth=0)
    assert (g["nodes"][0]["x"], g["nodes"][0]["y"]) == (42.0, -13.5)


def test_positions_survive_an_entity_update(store):
    """Layout is written by a different caller than entity writes, so an edit
    must not blank it."""
    chain(store, 2)
    store.manifest.set_positions("u", {"concept/n0": (1.0, 2.0)})
    store.flush()
    store.upsert_entity("u", "concept", "N0", summary_append="edited")
    store.flush()

    entry = store.manifest.get_entry("u", "concept/n0")
    assert (entry.x, entry.y) == (1.0, 2.0)


def test_unknown_ids_are_ignored_not_created(store):
    chain(store, 2)
    assert store.manifest.set_positions("u", {"concept/nope": (1.0, 1.0)}) == 0
    assert store.manifest.get_entry("u", "concept/nope") is None


# ---------- catalogue paging ----------

def test_catalogue_pages_and_searches(client):
    u = "/v1/users/demo/wiki"
    for i in range(25):
        client.put(u, json={"type": "concept", "title": f"Concept {i:02d}"})
    client.put(u, json={"type": "person", "title": "Alice Chen", "aliases": ["Ali"]})

    assert len(client.get(u).json()) == 26, "omitting limit must stay unpaged"
    assert len(client.get(u + "?limit=10").json()) == 10
    assert len(client.get(u + "?limit=10&offset=20").json()) == 6
    assert [e["wiki_id"] for e in client.get(u + "?q=alice").json()] == ["person/alice-chen"]
    assert [e["wiki_id"] for e in client.get(u + "?q=ali").json()] == ["person/alice-chen"], \
        "search should cover aliases"


def test_subgraph_endpoint_over_the_api(client):
    u = "/v1/users/demo"
    client.put(u + "/wiki", json={"type": "person", "title": "Alice Chen"})
    client.put(u + "/wiki", json={"type": "project", "title": "Orion"})
    client.post(u + "/wiki/person/alice-chen/relations",
                json={"target_wiki_id": "project/orion", "label": "leads"})

    g = client.post(u + "/wiki/subgraph",
                    json={"entry_wiki_ids": ["person/alice-chen"], "max_depth": 1}).json()
    assert {n["wiki_id"] for n in g["nodes"]} == {"person/alice-chen", "project/orion"}
    assert g["edges"] == [{"source": "person/alice-chen", "target": "project/orion",
                           "category": "related_to"}]

    r = client.post(u + "/wiki/layout", json={"positions": {"person/alice-chen": [5, 6]}})
    assert r.json() == {"updated": 1}


def test_subgraph_response_carries_component_and_layout_fields_over_http(client):
    """Regression: a dead, untyped duplicate handler used to shadow the real
    (response_model=SubgraphResponse) one, so x/y/degree/hidden_neighbours
    were silently dropped before ever reaching an HTTP caller even though
    the store computed them. Assert they actually survive the real request."""
    u = "/v1/users/demo"
    client.put(u + "/wiki", json={"type": "person", "title": "Alice Chen"})
    client.put(u + "/wiki", json={"type": "project", "title": "Orion"})
    client.post(u + "/wiki/person/alice-chen/relations",
               json={"target_wiki_id": "project/orion"})
    client.post(u + "/wiki/layout", json={"positions": {"person/alice-chen": [5, 6]}})

    g = client.post(u + "/wiki/subgraph",
                    json={"entry_wiki_ids": ["person/alice-chen"], "max_depth": 1}).json()
    alice = next(n for n in g["nodes"] if n["wiki_id"] == "person/alice-chen")
    assert isinstance(alice["component"], int)
    assert alice["degree"] == 1
    assert (alice["x"], alice["y"]) == (5.0, 6.0)


def test_exactly_one_subgraph_route_is_registered():
    """Regression guard: two @router.post("/wiki/subgraph") handlers were
    both registered under the same path -- Starlette silently matched the
    first (untyped, dropping fields) and the second (correctly typed) was
    dead code for the app's entire history. Nothing caught it."""
    from app.main import create_app
    app = create_app()

    matches = []
    for included in app.routes:
        original_router = getattr(included, "original_router", None)
        if original_router is None:
            continue
        for r in original_router.routes:
            if (getattr(r, "path", "").endswith("/wiki/subgraph")
                    and "POST" in (getattr(r, "methods", None) or [])):
                matches.append(r)
    assert len(matches) == 1, [getattr(r, "path", None) for r in matches]
