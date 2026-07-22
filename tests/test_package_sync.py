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
    assert package.voice_minutes is None
    assert package.text_sms is None


@pytest.mark.django_db
def test_package_sync_upserts_voice_and_text(
    sample_package_dto: PackageDTO,
) -> None:
    dto = PackageDTO(
        external_id="pkg-us-20gb-voice",
        title="20 GB - 365 Days",
        operator_title=sample_package_dto.operator_title,
        operator_id=sample_package_dto.operator_id,
        country_code="US",
        data_allowance="20 GB",
        validity_days=365,
        price_usd=Decimal("49.00"),
        net_price_usd=None,
        is_unlimited=False,
        plan_type="data",
        voice_minutes=200,
        text_sms=200,
    )
    provider = FakePackageProvider([dto])
    service = PackageSyncService(provider)

    service.sync()

    package = Package.objects.get(external_id="pkg-us-20gb-voice")
    assert package.voice_minutes == 200
    assert package.text_sms == 200


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
        coverages=(
            {
                "code": "US",
                "name": "United States",
                "networks": [
                    {"name": "T-Mobile", "types": ["5G"]},
                    {"name": "AT&T", "types": ["LTE"]},
                ],
            },
        ),
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
    assert location.coverages == [
        {
            "code": "US",
            "name": "United States",
            "networks": [
                {"name": "T-Mobile", "types": ["5G"]},
                {"name": "AT&T", "types": ["LTE"]},
            ],
        },
    ]
    assert location.is_popular is True

    package = Package.objects.get(external_id="pkg-us-1gb-7d")
    assert package.location_id == location.id


@pytest.mark.django_db
def test_package_sync_upserts_regional_coverages() -> None:
    dto = PackageDTO(
        external_id="europe-10gb-30d",
        title="10 GB - 30 Days",
        operator_title="Eurolink",
        operator_id="op-eu",
        country_code="",
        data_allowance="10 GB",
        validity_days=30,
        price_usd=Decimal("29.00"),
        net_price_usd=None,
        is_unlimited=False,
        plan_type="data",
        location_slug="europe",
        location_title="Europe",
        location_image_url="https://cdn.example.com/eu.png",
        coverage_type="regional",
        covered_country_codes=("HR", "DE"),
        coverages=(
            {
                "code": "HR",
                "name": "Croatia",
                "networks": [{"name": "Telemach", "types": ["5G"]}],
            },
            {
                "code": "DE",
                "name": "Germany",
                "networks": [{"name": "Telekom", "types": ["LTE"]}],
            },
        ),
    )
    provider = FakePackageProvider([dto])
    service = PackageSyncService(provider)

    service.sync()

    from apps.catalog.models import Location

    location = Location.objects.get(slug="europe")
    assert location.coverage_type == "regional"
    assert location.country_code == ""
    assert location.covered_country_codes == ["HR", "DE"]
    assert len(location.coverages) == 2
    assert location.coverages[0]["code"] == "HR"
    assert location.coverages[1]["name"] == "Germany"


@pytest.mark.django_db
def test_package_sync_renames_world_location_to_global() -> None:
    from apps.catalog.models import Location

    legacy = Location.objects.create(
        slug="world",
        title="Discover Global",
        coverage_type=Location.COVERAGE_GLOBAL,
        covered_country_codes=["US", "JP"],
        is_popular=True,
    )
    Package.objects.create(
        external_id="discover-in-7days-1gb-px",
        title="1 GB - 7 days",
        operator_title="Discover",
        operator_id="1525",
        country_code="",
        location=legacy,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("8.50"),
        is_active=True,
        synced_at=datetime.now(UTC),
    )

    dto = PackageDTO(
        external_id="discover-in-7days-1gb-px",
        title="1 GB - 7 days",
        operator_title="Discover",
        operator_id="1525",
        country_code="",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("8.50"),
        net_price_usd=None,
        is_unlimited=False,
        plan_type="data",
        location_slug="global",
        location_title="Discover Global",
        location_image_url="https://cdn.example.com/world.png",
        coverage_type="global",
        covered_country_codes=("US", "JP"),
    )
    service = PackageSyncService(FakePackageProvider([dto]))
    service.sync()

    assert not Location.objects.filter(slug="world").exists()
    location = Location.objects.get(slug="global")
    assert location.id == legacy.id
    assert location.title == "Discover Global"
    package = Package.objects.get(external_id="discover-in-7days-1gb-px")
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
