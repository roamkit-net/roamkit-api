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
    def list_packages(self, *, country_code: str | None = None):
        return [
            {
                "slug": "united-states",
                "country_code": "US",
                "title": "United States",
                "operators": [
                    {
                        "id": 123,
                        "title": "Change",
                        "plan_type": "data",
                        "countries": [{"country_code": "US", "title": "United States"}],
                        "packages": [
                            {
                                "id": "change-7days-1gb",
                                "title": "1 GB - 7 Days",
                                "data": "1 GB",
                                "day": 7,
                                "is_unlimited": False,
                                "price": 11.5,
                                "net_price": 6.3,
                                "prices": {
                                    "recommended_retail_price": {"USD": 11.5},
                                    "net_price": {"USD": 6.3},
                                },
                            }
                        ],
                    }
                ],
            }
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


def test_airalo_provider_maps_operator_packages() -> None:
    provider = AiraloPackageProvider(client=_FakePackageClient())

    packages = provider.list_packages(PackageFilters())

    assert len(packages) == 1
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
        result.installation_guide_url
        == "https://sandbox.airalo.com/installation-guide"
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
