"""Package sync service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from django.db import transaction

from apps.catalog.models import Location, Package
from shared.events.catalog_events import PackagesSynced
from shared.events.event_bus import event_bus
from shared.providers.esim import PackageDTO, PackageFilters, PackageProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

# Curated popular destinations (ISO2 country codes for local locations).
POPULAR_COUNTRY_CODES = frozenset(
    {
        "US",
        "JP",
        "GB",
        "IT",
        "CA",
        "FR",
        "ES",
        "DE",
        "TR",
        "TH",
        "AU",
        "MX",
        "KR",
        "CN",
        "AE",
        "PT",
        "NL",
        "CH",
        "BR",
        "IN",
    }
)

POPULAR_LOCATION_SLUGS = frozenset(
    {
        "united-states",
        "japan",
        "united-kingdom",
        "italy",
        "canada",
        "france",
        "spain",
        "germany",
        "turkey",
        "thailand",
        "australia",
        "mexico",
        "south-korea",
        "china",
        "united-arab-emirates",
        "portugal",
        "netherlands",
        "switzerland",
        "brazil",
        "india",
        "europe",
        "world",
    }
)


class PackageSyncService:
    """Syncs packages from a provider into the local catalog."""

    def __init__(self, provider: PackageProvider, *, source: str = "airalo") -> None:
        self.provider = provider
        self.source = source

    def sync(self, filters: PackageFilters | None = None) -> int:
        """Fetch packages from the provider and upsert them locally."""
        filters = filters or PackageFilters()
        packages = self.provider.list_packages(filters)
        synced_at = datetime.now(UTC)

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
        location_cache: dict[str, Location] = {}

        for dto in packages:
            location = self._upsert_location(dto, location_cache)
            Package.objects.update_or_create(
                external_id=dto.external_id,
                defaults={
                    "title": dto.title,
                    "operator_title": dto.operator_title,
                    "operator_id": dto.operator_id,
                    "country_code": dto.country_code,
                    "location": location,
                    "data_allowance": dto.data_allowance,
                    "validity_days": dto.validity_days,
                    "price_usd": dto.price_usd,
                    "net_price_usd": dto.net_price_usd,
                    "is_unlimited": dto.is_unlimited,
                    "plan_type": dto.plan_type,
                    "voice_minutes": dto.voice_minutes,
                    "text_sms": dto.text_sms,
                    "source": self.source,
                    "is_active": True,
                    "synced_at": synced_at,
                },
            )
            synced_ids.add(dto.external_id)

        return synced_ids

    def _upsert_location(
        self, dto: PackageDTO, cache: dict[str, Location]
    ) -> Location | None:
        slug = (dto.location_slug or "").strip()
        if not slug:
            return None

        if slug in cache:
            return cache[slug]

        coverage_type = dto.coverage_type or Location.COVERAGE_LOCAL
        if coverage_type not in {
            Location.COVERAGE_LOCAL,
            Location.COVERAGE_REGIONAL,
            Location.COVERAGE_GLOBAL,
        }:
            coverage_type = Location.COVERAGE_LOCAL

        country_code = dto.country_code.upper() if dto.country_code else ""
        if coverage_type != Location.COVERAGE_LOCAL:
            # Regional/global locations are not tied to a single ISO2 code.
            country_code = ""

        covered = list(dto.covered_country_codes)
        is_popular = (
            slug in POPULAR_LOCATION_SLUGS or country_code in POPULAR_COUNTRY_CODES
        )

        location, _ = Location.objects.update_or_create(
            slug=slug,
            defaults={
                "title": dto.location_title or slug,
                "country_code": country_code,
                "coverage_type": coverage_type,
                "image_url": dto.location_image_url or "",
                "covered_country_codes": covered,
                "is_popular": is_popular,
            },
        )
        cache[slug] = location
        return location

    def _deactivate_missing(self, synced_ids: set[str], synced_at: datetime) -> None:
        if not synced_ids:
            return

        (
            Package.objects.filter(source=self.source, is_active=True)
            .exclude(external_id__in=synced_ids)
            .update(is_active=False, synced_at=synced_at)
        )
