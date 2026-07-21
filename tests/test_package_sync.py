"""Tests for package sync service."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apps.catalog.models import Package
from apps.catalog.services.package_sync import PackageSyncService
from shared.events.catalog_events import PackagesSynced
from shared.providers.esim import PackageDTO, PackageFilters


class FakePackageProvider:
    def __init__(self, packages: list[PackageDTO] | None = None) -> None:
        self.packages = packages or []
        self.filters_received: PackageFilters | None = None

    def list_packages(self, filters: PackageFilters) -> list[PackageDTO]:
        self.filters_received = filters
        return self.packages


@pytest.fixture
def sample_package_dto() -> PackageDTO:
    return PackageDTO(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Change",
        operator_id="op-1",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        net_price_usd=Decimal("6.30"),
        is_unlimited=False,
        plan_type="data",
    )


@pytest.mark.django_db
def test_package_sync_upserts_packages(sample_package_dto: PackageDTO) -> None:
    provider = FakePackageProvider([sample_package_dto])
    service = PackageSyncService(provider)

    count = service.sync()

    assert count == 1
    package = Package.objects.get(external_id="pkg-us-1gb-7d")
    assert package.title == "1 GB - 7 Days"
    assert package.country_code == "US"
    assert package.is_active is True


@pytest.mark.django_db
def test_package_sync_deactivates_missing_packages(
    sample_package_dto: PackageDTO,
) -> None:
    Package.objects.create(
        external_id="stale-package",
        title="Old plan",
        operator_title="Old operator",
        country_code="DE",
        data_allowance="500 MB",
        validity_days=3,
        price_usd=Decimal("5.00"),
        is_active=True,
        synced_at=datetime.now(timezone.utc),
    )

    provider = FakePackageProvider([sample_package_dto])
    service = PackageSyncService(provider)
    service.sync()

    stale = Package.objects.get(external_id="stale-package")
    assert stale.is_active is False


@pytest.mark.django_db
def test_package_sync_publishes_event(sample_package_dto: PackageDTO) -> None:
    from shared.events.event_bus import event_bus

    received: list[PackagesSynced] = []
    event_bus.subscribe(PackagesSynced, received.append)

    provider = FakePackageProvider([sample_package_dto])
    service = PackageSyncService(provider)
    service.sync()

    assert len(received) == 1
    assert received[0].package_count == 1
    assert received[0].source == "airalo"
