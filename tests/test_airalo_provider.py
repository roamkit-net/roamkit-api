"""Tests for Airalo provider mapping."""

from decimal import Decimal
from typing import Any

from apps.integrations.airalo.providers import (
    AiraloOrderProvider,
    AiraloPackageProvider,
    AiraloTopupProvider,
)
from shared.providers.esim import PackageFilters


class _FakePackageClient:
    def __init__(self) -> None:
        self.list_packages_calls: list[dict[str, str | None]] = []

    def list_packages(
        self,
        *,
        country_code: str | None = None,
        package_type: str | None = None,
    ):
        self.list_packages_calls.append(
            {"country_code": country_code, "package_type": package_type}
        )
        return [
            {
                "slug": "united-states",
                "country_code": "US",
                "title": "United States",
                "image": {"url": "https://cdn.example.com/us.png"},
                "operators": [
                    {
                        "id": 123,
                        "title": "Change",
                        "type": "local",
                        "plan_type": "data",
                        "countries": [{"country_code": "US", "title": "United States"}],
                        "coverages": [
                            {
                                "name": "United States",
                                "code": "US",
                                "networks": [
                                    {"name": "T-Mobile", "types": ["5G", "LTE"]},
                                    {"name": "AT&T", "types": ["LTE"]},
                                ],
                            }
                        ],
                        "packages": [
                            {
                                "id": "change-7days-1gb",
                                "title": "1 GB - 7 Days",
                                "data": "1 GB",
                                "day": 7,
                                "is_unlimited": False,
                                "voice": None,
                                "text": None,
                                "price": 11.5,
                                "net_price": 6.3,
                                "prices": {
                                    "recommended_retail_price": {"USD": 11.5},
                                    "net_price": {"USD": 6.3},
                                },
                            },
                            {
                                "id": "change-20gb-365d-voice",
                                "title": "20 GB - 365 Days",
                                "data": "20 GB",
                                "day": 365,
                                "is_unlimited": False,
                                "voice": 200,
                                "text": 200,
                                "price": 49.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 49.0},
                                },
                            },
                            {
                                "id": "change-1gb-title-voice",
                                "title": "1 GB - 10 SMS - 10 Mins - 7 days",
                                "data": "1 GB",
                                "day": 7,
                                "is_unlimited": False,
                                "voice": None,
                                "text": None,
                                "price": 15.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 15.0},
                                },
                            },
                            {
                                "id": "change-7days-unlimited",
                                "title": "Unlimited - 7 Days",
                                "data": "Unlimited",
                                "day": 7,
                                "is_unlimited": True,
                                "price": 21.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 21.0},
                                },
                            },
                            {
                                "id": "change-10days-unlimited-no-flag",
                                "title": "Unlimited - 10 Days",
                                "data": "Unlimited",
                                "day": 10,
                                "is_unlimited": False,
                                "price": 28.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 28.0},
                                },
                            },
                        ],
                    }
                ],
            },
            {
                "slug": "europe",
                "country_code": "",
                "title": "Europe",
                "image": {"url": "https://cdn.example.com/eu.png"},
                "operators": [
                    {
                        "id": 456,
                        "title": "Eurolink",
                        "type": "global",
                        "plan_type": "data",
                        "countries": [
                            {"country_code": "HR", "title": "Croatia"},
                            {"country_code": "DE", "title": "Germany"},
                        ],
                        "coverages": [
                            {
                                "name": "Croatia",
                                "code": "HR",
                                "networks": [
                                    {"name": "Telemach", "types": ["5G"]},
                                    {"name": "A1 Hrvatska", "types": ["LTE"]},
                                ],
                            },
                            {
                                "name": "Germany",
                                "code": "DE",
                                "networks": [
                                    {"name": "Telekom", "types": ["5G", "LTE"]},
                                ],
                            },
                            {
                                # Duplicate code — networks should merge.
                                "name": "Croatia",
                                "code": "HR",
                                "networks": [
                                    {"name": "Telemach", "types": ["LTE"]},
                                    {"name": "Hrvatski Telekom", "types": ["LTE"]},
                                ],
                            },
                        ],
                        "packages": [
                            {
                                "id": "europe-10gb-30d",
                                "title": "10 GB - 30 Days",
                                "data": "10 GB",
                                "day": 30,
                                "is_unlimited": False,
                                "price": 29.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 29.0},
                                },
                            },
                            {
                                "id": "europe-7days-unlimited",
                                "title": "Unlimited - 7 Days",
                                "data": "Unlimited",
                                "day": 7,
                                "is_unlimited": True,
                                "price": 35.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 35.0},
                                },
                            },
                        ],
                    }
                ],
            },
            {
                "slug": "discover",
                "country_code": "",
                "title": "Discover Global",
                "image": {"url": "https://cdn.example.com/world.png"},
                "operators": [
                    {
                        "id": 789,
                        "title": "Discover",
                        "type": "global",
                        "plan_type": "data",
                        "countries": [
                            {"country_code": "US", "title": "United States"},
                            {"country_code": "JP", "title": "Japan"},
                        ],
                        "packages": [
                            {
                                "id": "world-20gb-30d",
                                "title": "20 GB - 30 Days",
                                "data": "20 GB",
                                "day": 30,
                                "is_unlimited": False,
                                "price": 69.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 69.0},
                                },
                            },
                            {
                                "id": "discover-in-7days-unlimited",
                                "title": "Unlimited - 7 days",
                                "data": "Unlimited",
                                "day": 7,
                                "is_unlimited": False,
                                "price": 45.0,
                                "prices": {
                                    "recommended_retail_price": {"USD": 45.0},
                                },
                            },
                        ],
                    }
                ],
            },
        ]


class _FakeOrderClient:
    def __init__(self) -> None:
        self.create_order_calls: list[dict[str, Any]] = []

    def create_order(
        self,
        *,
        package_id: str,
        quantity: int = 1,
        description: str = "",
    ) -> dict[str, Any]:
        self.create_order_calls.append(
            {
                "package_id": package_id,
                "quantity": quantity,
                "description": description,
            }
        )
        return {
            "id": 9666,
            "code": "20230227-009666",
            "package_id": package_id,
            "quantity": "1",
            "type": "sim",
            "description": description,
            "currency": "USD",
            "price": 9.5,
            "manual_installation": "<p>Manual</p>",
            "qrcode_installation": "<p>QR</p>",
            "installation_guides": {
                "en": "https://sandbox.airalo.com/installation-guide"
            },
            "sims": [
                {
                    "id": 11047,
                    "iccid": "891000000000009125",
                    "lpa": "lpa.airalo.com",
                    "matching_id": "TEST",
                    "qrcode": "LPA:1$lpa.airalo.com$TEST",
                    "qrcode_url": "https://sandbox.airalo.com/qr?id=1",
                    "direct_apple_installation_url": (
                        "https://esimsetup.apple.com/esim_qrcode_provisioning"
                        "?carddata=LPA:1$lpa.airalo.com$TEST"
                    ),
                }
            ],
        }


class _FakeTopupClient:
    def __init__(self) -> None:
        self.submit_topup_calls: list[dict[str, Any]] = []
        self.usage_calls: list[str] = []
        self.list_topups_calls: list[str] = []
        self.list_sim_packages_calls: list[str] = []

    def list_topups(self, iccid: str) -> list[dict[str, Any]]:
        self.list_topups_calls.append(iccid)
        return [
            {
                "id": "change-7days-1gb-topup",
                "type": "topup",
                "price": 4.5,
                "amount": 1024,
                "day": 7,
                "is_unlimited": False,
                "title": "1 GB - 7 Days",
                "data": "1 GB",
                "short_info": None,
                "voice": 0,
                "text": 0,
                "net_price": 3.6,
            }
        ]

    def submit_topup(
        self,
        *,
        iccid: str,
        package_id: str,
        description: str = "",
    ) -> dict[str, Any]:
        self.submit_topup_calls.append(
            {
                "iccid": iccid,
                "package_id": package_id,
                "description": description,
            }
        )
        return {
            "id": 111,
            "code": "20251118-000111",
            "package_id": package_id,
            "currency": "USD",
            "quantity": 1,
            "type": "topup",
            "description": description or f"Topup ({iccid})",
            "price": 4.5,
            "net_price": 3.6,
        }

    def get_usage(self, iccid: str) -> dict[str, Any]:
        self.usage_calls.append(iccid)
        return {
            "remaining": 767,
            "total": 2048,
            "expired_at": "2022-01-01 00:00:00",
            "is_unlimited": False,
            "status": "ACTIVE",
            "remaining_voice": 0,
            "remaining_text": 0,
            "total_voice": 0,
            "total_text": 0,
        }

    def list_sim_packages(self, iccid: str) -> list[dict[str, Any]]:
        self.list_sim_packages_calls.append(iccid)
        return [
            {
                "id": 728,
                "status": "ACTIVE",
                "remaining": 2378,
                "activated_at": "2023-01-09T10:30:45+00:00",
                "expired_at": "2023-02-09T10:30:45+00:00",
                "finished_at": None,
                "package": {
                    "id": "bonbon-mobile-30days-3gb-topup",
                    "type": "topup",
                    "price": 10,
                    "net_price": 6,
                    "amount": 3072,
                    "day": 30,
                    "is_unlimited": False,
                    "title": "3 GB - 30 Days",
                    "data": "3 GB",
                    "short_info": None,
                },
            },
            {
                "id": 729,
                "status": "WEIRD_NEW_STATUS",
                "remaining": 0,
                "activated_at": None,
                "expired_at": None,
                "finished_at": None,
                "order_id": "ord-99",
                "package": {
                    "id": "change-7days-unlimited",
                    "type": "sim",
                    "price": 1,
                    "net_price": 0.5,
                    "amount": 0,
                    "day": 7,
                    "is_unlimited": True,
                    "title": "Unlimited - 7 Days",
                    "data": "Unlimited",
                },
            },
        ]


def test_airalo_provider_maps_operator_packages() -> None:
    client = _FakePackageClient()
    provider = AiraloPackageProvider(client=client)

    packages = provider.list_packages(PackageFilters())

    assert client.list_packages_calls == [
        {"country_code": None, "package_type": "local"},
        {"country_code": None, "package_type": "global"},
    ]
    assert len(packages) == 9
    package = packages[0]
    assert package.external_id == "change-7days-1gb"
    assert package.title == "1 GB - 7 Days"
    assert package.operator_title == "Change"
    assert package.country_code == "US"
    assert package.data_allowance == "1 GB"
    assert package.validity_days == 7
    assert package.price_usd == Decimal("11.50")
    assert package.net_price_usd == Decimal("6.30")
    assert package.is_unlimited is False
    assert package.plan_type == "data"
    assert package.voice_minutes is None
    assert package.text_sms is None
    assert package.location_slug == "united-states"
    assert package.location_title == "United States"
    assert package.location_image_url == "https://cdn.example.com/us.png"
    assert package.coverage_type == "local"
    assert package.covered_country_codes == ("US",)
    assert package.coverages == (
        {
            "code": "US",
            "name": "United States",
            "networks": [
                {"name": "T-Mobile", "types": ["5G", "LTE"]},
                {"name": "AT&T", "types": ["LTE"]},
            ],
        },
    )

    dct = next(pkg for pkg in packages if pkg.external_id == "change-20gb-365d-voice")
    assert dct.voice_minutes == 200
    assert dct.text_sms == 200
    assert dct.plan_type == "data"

    from_title = next(
        pkg for pkg in packages if pkg.external_id == "change-1gb-title-voice"
    )
    assert from_title.voice_minutes == 10
    assert from_title.text_sms == 10


def test_airalo_provider_resolves_is_unlimited_from_flag_and_data() -> None:
    provider = AiraloPackageProvider(client=_FakePackageClient())

    packages = provider.list_packages(PackageFilters())
    by_id = {pkg.external_id: pkg for pkg in packages}

    flagged = by_id["change-7days-unlimited"]
    assert flagged.is_unlimited is True
    assert flagged.data_allowance == "Unlimited"

    # Flag false but data/title say Unlimited — still treat as unlimited.
    inferred = by_id["change-10days-unlimited-no-flag"]
    assert inferred.is_unlimited is True
    assert inferred.data_allowance == "Unlimited"

    regional = by_id["europe-7days-unlimited"]
    assert regional.is_unlimited is True
    assert regional.coverage_type == "regional"

    discover = by_id["discover-in-7days-unlimited"]
    assert discover.is_unlimited is True
    assert discover.location_slug == "global"
    assert discover.coverage_type == "global"


def test_airalo_provider_maps_regional_and_global() -> None:
    provider = AiraloPackageProvider(client=_FakePackageClient())

    packages = provider.list_packages(PackageFilters())
    by_id = {pkg.external_id: pkg for pkg in packages}

    europe = by_id["europe-10gb-30d"]
    assert europe.country_code == ""
    assert europe.location_slug == "europe"
    assert europe.coverage_type == "regional"
    assert europe.covered_country_codes == ("HR", "DE")
    assert europe.location_image_url == "https://cdn.example.com/eu.png"
    assert len(europe.coverages) == 2
    croatia = europe.coverages[0]
    assert croatia["code"] == "HR"
    assert croatia["name"] == "Croatia"
    assert croatia["networks"] == [
        {"name": "Telemach", "types": ["5G", "LTE"]},
        {"name": "A1 Hrvatska", "types": ["LTE"]},
        {"name": "Hrvatski Telekom", "types": ["LTE"]},
    ]
    germany = europe.coverages[1]
    assert germany["code"] == "DE"
    assert germany["networks"] == [
        {"name": "Telekom", "types": ["5G", "LTE"]},
    ]

    world = by_id["world-20gb-30d"]
    assert world.location_slug == "global"
    assert world.location_title == "Discover Global"
    assert world.coverage_type == "global"
    assert world.covered_country_codes == ("US", "JP")
    assert world.coverages == ()
    assert world.is_unlimited is False


def test_airalo_provider_dedupes_local_and_global_catalog_fetches() -> None:
    client = _FakePackageClient()
    provider = AiraloPackageProvider(client=client)

    packages = provider.list_packages(PackageFilters())

    # Fake returns the same catalog for both type filters; merge must not double.
    assert len(packages) == 9
    assert len({pkg.external_id for pkg in packages}) == 9


def test_airalo_order_provider_maps_create_order() -> None:
    client = _FakeOrderClient()
    provider = AiraloOrderProvider(client=client)

    result = provider.create_order("kallur-digital-7days-1gb", "order-ref-1")

    assert client.create_order_calls == [
        {
            "package_id": "kallur-digital-7days-1gb",
            "quantity": 1,
            "description": "order-ref-1",
        }
    ]
    assert result.external_order_id == "9666"
    assert result.code == "20230227-009666"
    assert result.package_id == "kallur-digital-7days-1gb"
    assert result.customer_ref == "order-ref-1"
    assert result.currency == "USD"
    assert result.price_usd == Decimal("9.5")
    assert result.manual_installation == "<p>Manual</p>"
    assert result.qrcode_installation == "<p>QR</p>"
    assert (
        result.installation_guide_url == "https://sandbox.airalo.com/installation-guide"
    )
    assert len(result.sims) == 1
    sim = result.sims[0]
    assert sim.iccid == "891000000000009125"
    assert sim.lpa == "lpa.airalo.com"
    assert sim.matching_id == "TEST"
    assert sim.qrcode == "LPA:1$lpa.airalo.com$TEST"
    assert sim.qrcode_url == "https://sandbox.airalo.com/qr?id=1"
    assert "esimsetup.apple.com" in sim.direct_apple_installation_url


def test_airalo_topup_provider_maps_list_topups() -> None:
    client = _FakeTopupClient()
    provider = AiraloTopupProvider(client=client)

    packages = provider.list_topups("891000000000009125")

    assert client.list_topups_calls == ["891000000000009125"]
    assert len(packages) == 1
    package = packages[0]
    assert package.external_id == "change-7days-1gb-topup"
    assert package.title == "1 GB - 7 Days"
    assert package.data_allowance == "1 GB"
    assert package.validity_days == 7
    assert package.price_usd == Decimal("4.5")
    assert package.net_price_usd == Decimal("3.6")
    assert package.is_unlimited is False
    assert package.plan_type == "topup"


def test_airalo_topup_provider_maps_submit_topup() -> None:
    client = _FakeTopupClient()
    provider = AiraloTopupProvider(client=client)

    result = provider.submit_topup("891000000000009125", "change-7days-1gb-topup")

    assert len(client.submit_topup_calls) == 1
    assert client.submit_topup_calls[0]["iccid"] == "891000000000009125"
    assert client.submit_topup_calls[0]["package_id"] == "change-7days-1gb-topup"
    assert result.external_order_id == "111"
    assert result.code == "20251118-000111"
    assert result.package_id == "change-7days-1gb-topup"
    assert result.iccid == "891000000000009125"
    assert result.currency == "USD"
    assert result.price_usd == Decimal("4.5")
    assert result.customer_ref == "Topup (891000000000009125)"


def test_airalo_topup_provider_maps_usage() -> None:
    client = _FakeTopupClient()
    provider = AiraloTopupProvider(client=client)

    usage = provider.get_usage("891000000000009125")

    assert client.usage_calls == ["891000000000009125"]
    assert usage.remaining_mb == 767
    assert usage.total_mb == 2048
    assert usage.expired_at == "2022-01-01 00:00:00"
    assert usage.is_unlimited is False
    assert usage.status == "ACTIVE"
    assert usage.remaining_voice == 0
    assert usage.remaining_text == 0
    assert usage.total_voice == 0
    assert usage.total_text == 0


def test_airalo_topup_provider_maps_sim_package_history() -> None:
    client = _FakeTopupClient()
    provider = AiraloTopupProvider(client=client)

    packages = provider.list_sim_packages("891000000000009125")

    assert client.list_sim_packages_calls == ["891000000000009125"]
    assert len(packages) == 2
    first = packages[0]
    assert first.instance_id == "728"
    assert first.status == "active"
    assert first.remaining_mb == 2378
    assert first.package_external_id == "bonbon-mobile-30days-3gb-topup"
    assert first.plan_type == "topup"
    assert first.data_allowance == "3 GB"
    assert first.validity_days == 30
    assert first.is_unlimited is False
    assert first.provider_order_id is None
    assert not hasattr(first, "price")
    assert not hasattr(first, "net_price")

    second = packages[1]
    assert second.status == "unknown"
    assert second.is_unlimited is True
    assert second.remaining_mb is None
    assert second.plan_type == "sim"
    assert second.provider_order_id == "ord-99"
