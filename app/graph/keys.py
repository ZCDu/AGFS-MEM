"""
Storage key construction for wiki data.

WHY THIS IS ONE FUNCTION AND NOT TWELVE F-STRINGS
    The layout is `{user_id}/wiki/...` -- one user, one wiki, storage keyed by
    the user the same way sessions and files already are. That path is built
    in twelve places across four modules, so changing it meant twelve chances
    to miss one -- and a missed site does not fail loudly, it silently reads
    and writes the wrong prefix. Everything routes through here now.

WHAT `scope` MEANS
    The wiki being addressed. The graph layer never resolves identity or
    checks permissions: it is handed a namespace and uses it. Access is
    decided once, in WikiRegistry.require(), before anything reaches this
    layer.

    In this single-wiki-per-user deployment `scope` is always the user_id --
    the parameter is named `scope` rather than `user_id` because the graph
    layer itself does not know or care that the two are the same thing.
"""

from __future__ import annotations


def wiki_prefix(scope: str) -> str:
    """`{scope}/wiki/` — every wiki object lives under this."""
    return f"{scope}/wiki/"


def wiki_key(scope: str, *parts: str) -> str:
    """Join parts under a wiki's prefix.

        wiki_key("demo", "person/alice.okf.md")
        -> "demo/wiki/person/alice.okf.md"
    """
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"{scope}/wiki/{tail}" if tail else wiki_prefix(scope)
