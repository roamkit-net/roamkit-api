"""Catalog domain events."""

from dataclasses import dataclass

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class PackagesSynced(DomainEvent):
    """Published after a successful package catalog sync."""

    package_count: int
    source: str
