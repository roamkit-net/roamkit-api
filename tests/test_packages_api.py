"""Tests for packages API endpoint."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.test import Client

from apps.catalog.models import Package


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def active_package() -> Package:
    return Package.objects.create(
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
        is_active=True,
        synced_at=datetime.now(UTC),
    )


@pytest.mark.django_db
def test_packages_list_returns_active_packages(
    client: Client, active_package: Package
) -> None:
    Package.objects.create(
        external_id="inactive-package",
        title="Inactive",
        operator_title="Change",
        country_code="US",
        data_allowance="500 MB",
        validity_days=3,
        price_usd=Decimal("4.00"),
        is_active=False,
        synced_at=datetime.now(UTC),
    )

    response = client.get("/api/v1/packages/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["id"] == active_package.external_id
    assert result["title"] == "1 GB - 7 Days"
    assert result["country_code"] == "US"
    assert result["price_usd"] == "11.50"
    assert result["list_price_usd"] == "11.50"
    assert result["discount_percent"] == "0.00"
    assert result["pricing_reason"] == "retail"
    assert result["voice_minutes"] is None
    assert result["text_sms"] is None
    assert "net_price_usd" not in result


@pytest.mark.django_db
def test_packages_list_includes_voice_and_text(client: Client) -> None:
    Package.objects.create(
        external_id="pkg-us-20gb-voice",
        title="20 GB - 365 Days",
        operator_title="Change",
        operator_id="op-1",
        country_code="US",
        data_allowance="20 GB",
        validity_days=365,
        price_usd=Decimal("49.00"),
        is_unlimited=False,
        plan_type="data",
        voice_minutes=200,
        text_sms=200,
        is_active=True,
        synced_at=datetime.now(UTC),
    )

    response = client.get("/api/v1/packages/")

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["id"] == "pkg-us-20gb-voice"
    assert result["voice_minutes"] == 200
    assert result["text_sms"] == 200


@pytest.mark.django_db
def test_packages_list_filters_by_country(client: Client) -> None:
    synced_at = datetime.now(UTC)
    Package.objects.create(
        external_id="pkg-us",
        title="US plan",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        is_active=True,
        synced_at=synced_at,
    )
    Package.objects.create(
        external_id="pkg-de",
        title="DE plan",
        operator_title="Change",
        country_code="DE",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("9.00"),
        is_active=True,
        synced_at=synced_at,
    )

    response = client.get("/api/v1/packages/?country=DE")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["results"][0]["country_code"] == "DE"
