"""
Connected-component detection over a wiki's entity graph.

WHY THIS EXISTS
    A wiki can hold several genuinely unrelated clusters of entities -- a
    "Sales Team" cluster and a "Marketing Campaign" cluster with zero edges
    between them, for instance. Nothing in the graph/search/chat layers
    previously had a concept of "these entities belong together, those
    don't"; a query ambiguous between two clusters got whatever the flat
    per-entity score happened to produce. This module makes cluster
    membership a first-class, reusable primitive, consumed by:
      - app/graph/store.py's subgraph() -- tags each returned node with its
        component so a client (the GUI) can lay out disconnected clusters
        separately instead of overlapping them.
      - app/api/routes_chat.py's _memory_context() and app/graph/search.py's
        rank_entries() -- soft-prioritize the cluster that scores more
        relevant to a query, without hard-filtering the other one out.

build_adjacency() is extracted (not duplicated) from what store.py's
subgraph() already built inline -- see that function's docstring for why
the adjacency is undirected even though ManifestEntry.edges is stored
directionally by default.
"""

from __future__ import annotations

from app.graph.manifest import ManifestEntry

# Reuses app/wikis/router.py's TIE_MARGIN=0.10 directly: same shape of
# decision (is one candidate meaningfully ahead of another) on the same
# 0-1 confidence scale LinkedEntity already uses -- no reason to diverge.
DEFAULT_TIE_MARGIN = 0.10

# A SOFT demotion between comparable-strength candidates in DIFFERENT
# components, not a tier separator. This is a different job from
# app/graph/search.py's _DESCRIPTION_MATCH_WEIGHT=0.5 (which deliberately
# keeps a whole tier from ever outranking a higher one) -- 0.6 is not
# derived from that constant. A strong match in a "losing" component should
# still surface, just ranked below an equally-strong match in the
# top-scoring component; a hard filter would risk dropping real content
# just because it happened to land in the less on-topic cluster.
DEFAULT_PENALTY = 0.6


def build_adjacency(entries: list[ManifestEntry],
                    categories: set[str] | None = None) -> dict[str, set[str]]:
    """Undirected adjacency over ACTIVE entities: {wiki_id: {neighbour_ids}}.

    Unions both directions of ManifestEntry.edges (outbound only by
    default) because "what is connected to this" is symmetric even though
    a relation is stored with a direction -- the same reasoning
    store.py's subgraph() documents for its own adjacency build.

    `categories` optionally restricts which edge categories count, so
    component membership stays consistent with whatever filtered view a
    caller (e.g. subgraph() with a category filter) is already computing
    degree/hidden_neighbours against, rather than introducing a second,
    inconsistent notion of "connected."
    """
    active = {e.wiki_id: e for e in entries if e.status == "active"}
    adj: dict[str, set[str]] = {w: set() for w in active}
    for wiki_id, entry in active.items():
        for edge in entry.edges or []:
            target, cat = edge.get("t"), edge.get("c", "related_to")
            if target not in active:
                continue
            if categories is not None and cat not in categories:
                continue
            adj[wiki_id].add(target)
            adj[target].add(wiki_id)
    return adj


def connected_components(adj: dict[str, set[str]]) -> list[list[str]]:
    """Partition wiki_ids into connected components via BFS.

    Sorted largest-first, then lexicographically by the component's
    smallest wiki_id, so the result (and therefore any component index
    derived from it) is deterministic across calls with the same input."""
    seen: set[str] = set()
    out: list[list[str]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        comp: list[str] = []
        queue = [start]
        seen.add(start)
        while queue:
            node = queue.pop()
            comp.append(node)
            for neighbour in adj.get(node, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        out.append(sorted(comp))
    out.sort(key=lambda c: (-len(c), c[0] if c else ""))
    return out


def component_priority_weights(
    components: list[list[str]], scores: dict[str, float],
    penalty: float = DEFAULT_PENALTY, tie_margin: float = DEFAULT_TIE_MARGIN,
) -> dict[str, float]:
    """A per-wiki_id multiplier: 1.0 for entities in the top-scoring
    component (or any component within `tie_margin` of it -- genuinely
    ambiguous queries must not unfairly penalize a component that's really
    just as relevant as the "winner"), `penalty` for everyone else.

    `scores` need only cover entities that already have a signal (e.g.
    LinkedEntity.confidence values already computed elsewhere) -- a
    component with no scored members simply never influences or receives a
    boost; it is neither the winner nor unfairly demoted relative to other
    unscored entities, since a weight only ever matters when multiplied
    against an existing score.

    With a SINGLE component, every wiki_id lands in that one component, its
    representative score trivially equals the best score, so every weight
    is 1.0 regardless of `scores` -- callers on a single-cluster wiki are
    provably unaffected, not just empirically so.
    """
    if not components:
        return {}

    representative: list[float] = []
    for comp in components:
        comp_scores = [scores[w] for w in comp if w in scores]
        representative.append(max(comp_scores) if comp_scores else 0.0)

    best = max(representative)
    weights: dict[str, float] = {}
    for comp, rep in zip(components, representative):
        weight = 1.0 if (best - rep) < tie_margin else penalty
        for wiki_id in comp:
            weights[wiki_id] = weight
    return weights
