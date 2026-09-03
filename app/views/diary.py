"""The scheduled diary pass: generate a daily diary for every user.

Called by the auto-capture timer after capture_day runs. It enumerates every
user who has a home wiki, and for each generates + persists a chronological
"diary" of that day's activity (via WikiSummarizer.generate_diary), stored
under `wikis/{user}/_diary/{day}.json`.

Best-effort by design: a failure for one user must never abort the pass for
the others, and a pass with no LLM key just skips quietly (the capture itself
already succeeded and is independent of this).
"""

from __future__ import annotations

import logging

from app.storage.backend import StorageBackend
from app.graph.store import EntityGraphStore
from app.wikis.registry import WikiRegistry
from app.views.summarizer import WikiSummarizer

logger = logging.getLogger("memory_backend.views")


def run_diary_pass(backend: StorageBackend, day,
                   summarizer_factory=None) -> dict:
    """Generate a diary for every user with a home wiki for `day`.

    `day` may be a datetime.date or an ISO string. Returns a dict keyed by
    user_id -> "generated" | "skipped" | "error" (for diagnostics). Best-effort
    and never raises. `summarizer_factory` is a test seam: callable(registry,
    store) -> WikiSummarizer, defaulting to the real LLM-backed one.
    """
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    registry = WikiRegistry(backend)
    store = EntityGraphStore(backend)
    summaries: dict[str, str] = {}
    def _build():
        if summarizer_factory is not None:
            return summarizer_factory(registry, store)
        return WikiSummarizer(registry, store)
    try:
        homes = [w for w in registry.list_all(include_archived=True)
                 if w.kind == "home"]
    except Exception:
        logger.exception("diary pass could not list home wikis")
        return {"error": "could not list home wikis"}
    for home in homes:
        owner = home.wiki_id
        try:
            summ = _build()
            rec = summ.generate_diary(owner, day=day_str, viewer="system")
            summaries[owner] = "generated" if rec else "skipped"
        except Exception as e:
            logger.warning("diary pass failed for %s on %s: %s", owner, day_str, e)
            summaries[owner] = "error"
    if summaries:
        logger.info("diary pass for %s: %s", day_str, summaries)
    return summaries
