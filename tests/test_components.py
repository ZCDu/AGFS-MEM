"""
Tests for app/graph/components.py -- connected-component detection over a
wiki's entity graph, and the priority weighting derived from it.

Pure unit tests against ManifestEntry lists directly; no store/backend
fixture needed since these are stateless functions over data already in
hand (the same manifest entries every caller already has loaded).
"""

from __future__ import annotations

from app.graph.components import (build_adjacency, component_priority_weights,
                                  connected_components)
from app.graph.manifest import ManifestEntry


def _entry(wiki_id: str, edges: list[dict] | None = None,
          status: str = "active") -> ManifestEntry:
    return ManifestEntry(wiki_id=wiki_id, type="concept", title=wiki_id,
                         edges=edges, status=status)


# ---------- build_adjacency ----------

def test_adjacency_is_undirected_given_a_one_directional_edge():
    entries = [_entry("a", edges=[{"t": "b", "c": "related_to"}]), _entry("b")]
    adj = build_adjacency(entries)
    assert adj == {"a": {"b"}, "b": {"a"}}


def test_adjacency_respects_a_category_filter():
    entries = [_entry("a", edges=[{"t": "b", "c": "causes"}]), _entry("b")]
    assert build_adjacency(entries, categories={"related_to"}) == {"a": set(), "b": set()}
    assert build_adjacency(entries, categories={"causes"}) == {"a": {"b"}, "b": {"a"}}


def test_isolated_entity_has_an_empty_adjacency_set():
    adj = build_adjacency([_entry("a")])
    assert adj == {"a": set()}


def test_adjacency_ignores_tombstoned_entities():
    entries = [_entry("a", edges=[{"t": "b", "c": "related_to"}]),
              _entry("b", status="deleted")]
    adj = build_adjacency(entries)
    assert adj == {"a": set()}


# ---------- connected_components ----------

def test_single_chain_is_one_component():
    adj = build_adjacency([
        _entry("a", edges=[{"t": "b", "c": "related_to"}]),
        _entry("b", edges=[{"t": "c", "c": "related_to"}]),
        _entry("c"),
    ])
    comps = connected_components(adj)
    assert comps == [["a", "b", "c"]]


def test_two_disjoint_edge_groups_are_two_components_with_no_cross_membership():
    adj = build_adjacency([
        _entry("a", edges=[{"t": "b", "c": "related_to"}]), _entry("b"),
        _entry("x", edges=[{"t": "y", "c": "related_to"}]), _entry("y"),
    ])
    comps = connected_components(adj)
    assert len(comps) == 2
    assert {"a", "b"} in (set(c) for c in comps)
    assert {"x", "y"} in (set(c) for c in comps)
    all_members = set(comps[0]) | set(comps[1])
    assert all_members == {"a", "b", "x", "y"}


def test_component_ordering_is_deterministic_largest_first():
    adj = build_adjacency([
        _entry("a", edges=[{"t": "b", "c": "related_to"}]),
        _entry("b", edges=[{"t": "c", "c": "related_to"}]),
        _entry("c"),
        _entry("x", edges=[{"t": "y", "c": "related_to"}]),
        _entry("y"),
    ])
    comps = connected_components(adj)
    assert comps == [["a", "b", "c"], ["x", "y"]]


def test_equal_size_components_break_ties_lexicographically():
    adj = build_adjacency([
        _entry("z", edges=[{"t": "y", "c": "related_to"}]), _entry("y"),
        _entry("a", edges=[{"t": "b", "c": "related_to"}]), _entry("b"),
    ])
    comps = connected_components(adj)
    assert comps == [["a", "b"], ["y", "z"]]


# ---------- component_priority_weights ----------

def test_single_component_every_weight_is_one_regardless_of_scores():
    comps = [["a", "b", "c"]]
    weights = component_priority_weights(comps, {"a": 0.95, "b": 0.1})
    assert weights == {"a": 1.0, "b": 1.0, "c": 1.0}


def test_clear_winner_outside_tie_margin_demotes_the_other_component():
    comps = [["a"], ["b"]]
    weights = component_priority_weights(
        comps, {"a": 0.9, "b": 0.2}, penalty=0.6, tie_margin=0.10)
    assert weights == {"a": 1.0, "b": 0.6}


def test_two_components_within_tie_margin_both_get_full_weight():
    comps = [["a"], ["b"]]
    weights = component_priority_weights(
        comps, {"a": 0.9, "b": 0.85}, penalty=0.6, tie_margin=0.10)
    assert weights == {"a": 1.0, "b": 1.0}


def test_component_with_no_scored_members_does_not_crash():
    comps = [["a"], ["b", "c"]]
    weights = component_priority_weights(comps, {"a": 0.9})
    assert weights["a"] == 1.0
    # b/c's component has representative score 0.0, far below 0.9 -> penalty.
    assert weights["b"] == weights["c"] == 0.6


def test_empty_components_list_returns_empty_weights():
    assert component_priority_weights([], {}) == {}
