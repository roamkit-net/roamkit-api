"""Tests for package sync service."""

from datetime import UTC, datetime
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
        synced_at=datetime.now(UTC),
    )

    provider = FakePackageProvider([sample_package_dto])
    service = PackageSyncService(provider)
    service.sync()

    stale = Package.objects.get(external_id="stale-package")
    assert stale.is_active is False


@pytest.mark.django_db
def test_package_sync_upserts_location(sample_package_dto: PackageDTO) -> None:
    dto = PackageDTO(
        external_id=sample_package_dto.external_id,
        title=sample_package_dto.title,
        operator_title=sample_package_dto.operator_title,
        operator_id=sample_package_dto.operator_id,
        country_code="US",
        data_allowance=sample_package_dto.data_allowance,
        validity_days=sample_package_dto.validity_days,
        price_usd=sample_package_dto.price_usd,
        net_price_usd=sample_package_dto.net_price_usd,
        is_unlimited=sample_package_dto.is_unlimited,
        plan_type=sample_package_dto.plan_type,
        location_slug="united-states",
        location_title="United States",
        location_image_url="https://cdn.example.com/us.png",
        coverage_type="local",
        covered_country_codes=("US",),
    )
    provider = FakePackageProvider([dto])
    service = PackageSyncService(provider)

    service.sync()

    from apps.catalog.models import Location

    location = Location.objects.get(slug="united-states")
    assert location.title == "United States"
    assert location.country_code == "US"
    assert location.coverage_type == "local"
    assert location.image_url == "https://cdn.example.com/us.png"
    assert location.covered_country_codes == ["US"]
    assert location.is_popular is True

    package = Package.objects.get(external_id="pkg-us-1gb-7d")
    assert package.location_id == location.id


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
