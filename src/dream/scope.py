"""Compatibility imports for :mod:`dream.core.scope`.

New code should import scope primitives from ``dream.core.scope``. This module
remains temporarily so existing integrations keep their public import paths
during the staged architecture migration.
"""

from dream.core.scope import ScopeIds, ScopePaths, resolve_scope

__all__ = ["ScopeIds", "ScopePaths", "resolve_scope"]
