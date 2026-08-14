"""
Storage key construction for wiki data.

WHY THIS IS ONE FUNCTION AND NOT TWELVE F-STRINGS
    The layout moved from `{user_id}/wiki/...` to `wikis/{scope}/...` when
    wikis stopped being owned by a user. That prefix was being built in twelve
    places across four modules, so changing it meant twelve chances to miss
    one — and a missed site does not fail loudly, it silently reads and writes
    the wrong prefix. Everything routes through here now.

WHAT `scope` MEANS
    The wiki being addressed. The graph layer never resolves identity or
    checks permissions: it is handed a namespace and uses it. Access is
    decided once, in WikiRegistry.require(), before anything reaches this
    layer.

    Before the multi-wiki change the caller passed a user_id, which is why the
    parameter is still named that in places — one user, one wiki. Callers now
    pass a wiki_id. The graph layer cannot tell the difference and does not
    need to.
"""

from __future__ import annotations

WIKI_ROOT = "wikis"


def wiki_prefix(scope: str) -> str:
    """`wikis/{scope}/` — every wiki object lives under this."""
    return f"{WIKI_ROOT}/{scope}/"


def wiki_key(scope: str, *parts: str) -> str:
    """Join parts under a wiki's prefix.

        wiki_key("sales", "person/alice.okf.md")
        -> "wikis/sales/person/alice.okf.md"
    """
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"{WIKI_ROOT}/{scope}/{tail}" if tail else wiki_prefix(scope)


def legacy_wiki_prefix(scope: str) -> str:
    """The pre-multi-wiki layout, `{user_id}/wiki/`.

    Kept so the migration can find old data, and so a deployment that has not
    migrated yet can still be read. Nothing writes here.
    """
    return f"{scope}/wiki/"
