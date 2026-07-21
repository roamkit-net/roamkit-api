"""Package sync service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from django.db import transaction

from apps.catalog.models import Package
from shared.events.catalog_events import PackagesSynced
from shared.events.event_bus import event_bus
from shared.providers.esim import PackageDTO, PackageFilters, PackageProvider

if TYPE_CHECKING:
    from collections.abc import Iterable


class PackageSyncService:
    """Syncs packages from a provider into the local catalog."""

    def __init__(self, provider: PackageProvider, *, source: str = "airalo") -> None:
        self.provider = provider
        self.source = source

    def sync(self, filters: PackageFilters | None = None) -> int:
        """Fetch packages from the provider and upsert them locally."""
        filters = filters or PackageFilters()
        packages = self.provider.list_packages(filters)
        synced_at = datetime.now(timezone.utc)

        with transaction.atomic():
            synced_ids = self._upsert_packages(packages, synced_at)
            self._deactivate_missing(synced_ids, synced_at)

        event_bus.publish(
            PackagesSynced(package_count=len(synced_ids), source=self.source)
        )
        return len(synced_ids)

    def _upsert_packages(
        self, packages: Iterable[PackageDTO], synced_at: datetime
    ) -> set[str]:
        synced_ids: set[str] = set()

        for dto in packages:
            Package.objects.update_or_create(
                external_id=dto.external_id,
                defaults={
                    "title": dto.title,
                    "operator_title": dto.operator_title,
                    "operator_id": dto.operator_id,
                    "country_code": dto.country_code,
                    "data_allowance": dto.data_allowance,
                    "validity_days": dto.validity_days,
                    "price_usd": dto.price_usd,
                    "net_price_usd": dto.net_price_usd,
                    "is_unlimited": dto.is_unlimited,
                    "plan_type": dto.plan_type,
                    "source": self.source,
                    "is_active": True,
                    "synced_at": synced_at,
                },
            )
            synced_ids.add(dto.external_id)

        return synced_ids

    def _deactivate_missing(self, synced_ids: set[str], synced_at: datetime) -> None:
        if not synced_ids:
            return

        (
            Package.objects.filter(source=self.source, is_active=True)
            .exclude(external_id__in=synced_ids)
            .update(is_active=False, synced_at=synced_at)
        )
