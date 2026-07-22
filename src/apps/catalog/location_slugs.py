"""Canonical location slug helpers."""

from __future__ import annotations

# Partner API uses "world"; store URLs use /global-esim.
_GLOBAL_ALIASES = frozenset(
    {
        "world",
        "global",
        "worldwide",
        "discover",
        "discover-global",
    }
)
_GLOBAL_CANONICAL = "global"


def resolve_location_slug(slug: str) -> str:
    """Map Discover Global aliases to the canonical catalog slug."""
    normalized = (slug or "").strip().lower()
    if normalized in _GLOBAL_ALIASES:
        return _GLOBAL_CANONICAL
    return normalized
