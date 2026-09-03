"""
Ranking for the wiki catalogue's `q` search param (GET /wiki, GET /entities).

Previously `q` only matched title/wiki_id/alias substrings, so a query like
"orion project timeline" found nothing unless that exact text appeared in a
node's name -- even when the node's own description obviously matched. This
adds a description-overlap tier on top of the existing substring match,
reusing the same lexical (non-LLM) tokenizer the chat-retrieval assessor
already uses against `compact` in app/verify/assessor.py.

A third, optional tier sits below both of those: semantic (embedding-based)
matching. Tier 1 (substring) is always the outright winner when it hits
anything -- unambiguous and free, so there's no reason to blend it with
anything else. Below that, tiers 2 (description overlap) and 3 (semantic)
are FUSED via reciprocal rank fusion (semantica.vector_store.SearchRanker)
rather than the old either/or ("try semantic only if description-overlap
found literally nothing"): a weak jaccard hit no longer outranks a strong
semantic match outright just because it ran first. This keeps the same
cost-control trigger as before -- the embedding call only happens when tier
1 found nothing, never on every query -- see app/graph/embeddings.py's
module docstring for why that boundary matters. Degrades silently (no
store/scope passed in, feature disabled, or the model unavailable) exactly
as before: fusion with an empty tier-3 list is just tier 2 alone.

Within whatever tier actually matched, results also get a soft priority
boost toward whichever disconnected cluster (see app/graph/components.py)
scores most relevant overall -- a query ambiguous between two unrelated
clusters in the same wiki should surface the more on-topic one first,
without hiding the other entirely. A no-op on a single-cluster wiki.
"""

from __future__ import annotations

from app.graph.lexical import content_tokens, jaccard
from app.graph.manifest import ManifestEntry

# Every substring (name) hit outranks every description-only hit: max score
# for a name hit is 1.0, max score for a description hit is 0.5 (the 0.5
# multiplier below), so the two tiers never interleave.
_DESCRIPTION_MATCH_WEIGHT = 0.5


def rank_entries(entries: list[ManifestEntry], q: str,
                 store=None, scope: str | None = None) -> list[ManifestEntry]:
    """Score entries against a search query and return them ranked, highest
    match first. A case-insensitive substring hit on title/wiki_id/alias is
    always a stronger signal than anything else and short-circuits; below
    that, description-overlap and semantic-similarity are fused (see module
    docstring) rather than one strictly gating the other.

    `store` and `scope` are optional and only used for the semantic fallback
    tier (store.embedding_index, scoped by wiki_id/user_id) -- omit them and
    this behaves exactly as the lexical-only version always has."""
    needle = q.strip().lower()
    if not needle:
        return entries

    substring_hits = [e for e in entries
                      if (needle in e.title.lower() or needle in e.wiki_id.lower()
                          or any(needle in a.lower() for a in e.aliases))]
    substring_ids = {e.wiki_id for e in substring_hits}

    q_tokens = content_tokens(q)
    description_hits: list[tuple[float, ManifestEntry]] = []
    for e in entries:
        if e.wiki_id in substring_ids:
            continue  # already scored via the stronger tier above
        if e.compact:
            overlap = jaccard(content_tokens(e.compact), q_tokens)
            if overlap > 0:
                description_hits.append((_DESCRIPTION_MATCH_WEIGHT * overlap, e))

    if substring_hits:
        # A substring hit anywhere is decisive: rank it above every
        # description-only hit (title matches always outscore 0.5 * overlap
        # <= 0.5), but entries that only matched on description still belong
        # in the results, just lower -- semantic is not consulted at all
        # here, matching the existing "lexical hit skips semantic" cost
        # invariant.
        combined = [(1.0, e) for e in substring_hits] + description_hits
        return _reweight_by_component(entries, combined)

    semantic_hits: list[tuple[float, ManifestEntry]] = []
    if entries and store is not None and scope is not None:
        semantic_hits = _semantic_candidates(entries, q, store, scope)

    if not description_hits and not semantic_hits:
        return []
    if description_hits and not semantic_hits:
        return _reweight_by_component(entries, description_hits)
    if semantic_hits and not description_hits:
        return _reweight_by_component(entries, semantic_hits)

    fused = _fuse(description_hits, semantic_hits)
    return _reweight_by_component(entries, fused)


_RRF_K = 60  # standard constant from the reciprocal rank fusion literature


def _fuse(description_hits: list[tuple[float, ManifestEntry]],
         semantic_hits: list[tuple[float, ManifestEntry]]) -> list[tuple[float, ManifestEntry]]:
    """Combine the two non-substring tiers with reciprocal rank fusion
    instead of picking one outright -- an entry both tiers agree on ranks
    above one only a single tier likes, and a strong semantic-only match is
    no longer hidden behind a merely-nonzero jaccard hit elsewhere.

    This is semantica's algorithm (semantica.vector_store.hybrid_search.
    SearchRanker.reciprocal_rank_fusion -- 1/(k+rank) per list, summed by
    id), reimplemented locally rather than imported: importing anything from
    semantica.vector_store eagerly loads its __init__.py, which imports
    EVERY vector-store backend it bundles (FAISS, Milvus, Pinecone, Qdrant,
    Weaviate...), each pulling in its own heavy, unrelated dependency
    (numpy, scipy, and more behind those) just to reach a ~15-line,
    dependency-free function. That defeats the actual goal here -- a
    stateless algorithm, not a new dependency footprint -- so the formula is
    copied, not the import."""
    scores: dict[str, float] = {}
    by_id: dict[str, ManifestEntry] = {}
    for hits in (description_hits, semantic_hits):
        for rank, (_, e) in enumerate(sorted(hits, key=lambda pair: -pair[0]), start=1):
            scores[e.wiki_id] = scores.get(e.wiki_id, 0.0) + 1.0 / (_RRF_K + rank)
            by_id[e.wiki_id] = e
    return [(score, by_id[wid]) for wid, score in scores.items()]


def _reweight_by_component(entries: list[ManifestEntry],
                           scored: list[tuple[float, ManifestEntry]]) -> list[ManifestEntry]:
    if len(scored) > 1:
        from app.graph.components import (build_adjacency,
                                          component_priority_weights,
                                          connected_components)
        adj = build_adjacency(entries)
        comps = connected_components(adj)
        weights = component_priority_weights(
            comps, {e.wiki_id: s for s, e in scored})
        scored = [(s * weights.get(e.wiki_id, 1.0), e) for s, e in scored]
    # Recency is a tie-breaker, not a score factor: it only decides ordering
    # between entries that already landed on the same relevance score (e.g.
    # two substring hits, or an RRF tie between description and semantic
    # tiers). It never lets a stale strong match lose to a fresher weak one --
    # that would undermine the tier ordering the rest of this module
    # maintains (see module docstring). Untouched-since-creation entries
    # (last_accessed == "") sort last within a tie rather than raising.
    # Recency is a tie-breaker, not a score factor: it only decides ordering
    # between entries that already landed on the same relevance score (e.g.
    # two substring hits, or an RRF tie between description and semantic
    # tiers). It never lets a stale strong match lose to a fresher weak one --
    # that would undermine the tier ordering the rest of this module
    # maintains (see module docstring). Untouched-since-creation entries
    # (last_accessed == "") sort last within a tie rather than raising.
    from app.graph.store import decay_score as _decay
    scored = sorted(
        scored,
        key=lambda pair: (-pair[0], -_decay(pair[1].last_accessed)),
    )
    return [e for _, e in scored]


def _semantic_candidates(entries: list[ManifestEntry], q: str,
                         store, scope: str) -> list[tuple[float, ManifestEntry]]:
    """Only reached when tier 1 (substring) found nothing (see rank_entries
    above) -- one embed call for the query, compared against vectors already
    cached in memory by EmbeddingIndex. No-ops (returns []) unless
    EMBEDDING_ENABLED is set and the model is actually available."""
    from app.graph.embeddings import cosine, embed, is_embedding_enabled

    if not is_embedding_enabled():
        return []
    q_vector = embed(q)
    if q_vector is None:
        return []
    vectors = getattr(store, "embedding_index", None)
    if vectors is None:
        return []
    vectors = vectors.get_all(scope)
    if not vectors:
        return []

    from app.config import get_settings
    floor = get_settings().embedding_similarity_floor

    by_id = {e.wiki_id: e for e in entries}
    scored: list[tuple[float, ManifestEntry]] = []
    for wiki_id, vector in vectors.items():
        entry = by_id.get(wiki_id)
        if entry is None:
            continue
        similarity = cosine(q_vector, vector)
        if similarity >= floor:
            scored.append((similarity, entry))

    return scored
