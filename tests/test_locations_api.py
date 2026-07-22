"""Tests for locations API endpoint."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from django.test import Client

from apps.catalog.models import Location, Package


@pytest.fixture
def client() -> Client:
    return Client()


def _create_package(
    *,
    external_id: str,
    location: Location,
    price: str = "11.50",
    country_code: str = "",
) -> Package:
    return Package.objects.create(
        external_id=external_id,
        title=f"{external_id} plan",
        operator_title="Change",
        operator_id="op-1",
        country_code=country_code or location.country_code,
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal(price),
        is_active=True,
        synced_at=datetime.now(timezone.utc),
    )


@pytest.mark.django_db
def test_locations_list_returns_min_price(client: Client) -> None:
    croatia = Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
        is_popular=False,
    )
    _create_package(external_id="hr-cheap", location=croatia, price="4.00")
    _create_package(external_id="hr-pricey", location=croatia, price="12.00")

    response = client.get("/api/v1/locations/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["slug"] == "croatia"
    assert result["title"] == "Croatia"
    assert result["min_price_usd"] == "4.00"
    assert result["coverage_type"] == "local"


@pytest.mark.django_db
def test_locations_list_filters_by_type(client: Client) -> None:
    local = Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
        is_popular=True,
    )
    regional = Location.objects.create(
        slug="europe",
        title="Europe",
        coverage_type=Location.COVERAGE_REGIONAL,
        covered_country_codes=["HR", "DE"],
        is_popular=False,
    )
    global_loc = Location.objects.create(
        slug="world",
        title="World",
        coverage_type=Location.COVERAGE_GLOBAL,
        covered_country_codes=["HR", "US"],
        is_popular=True,
    )
    _create_package(external_id="hr", location=local, price="5.00")
    _create_package(external_id="eu", location=regional, price="20.00")
    _create_package(external_id="world-pkg", location=global_loc, price="50.00")

    popular = client.get("/api/v1/locations/?type=popular").json()
    assert {item["slug"] for item in popular["results"]} == {"croatia", "world"}

    locals_only = client.get("/api/v1/locations/?type=local").json()
    assert [item["slug"] for item in locals_only["results"]] == ["croatia"]

    regionals = client.get("/api/v1/locations/?type=regional").json()
    assert [item["slug"] for item in regionals["results"]] == ["europe"]

    globals_only = client.get("/api/v1/locations/?type=global").json()
    assert [item["slug"] for item in globals_only["results"]] == ["world"]


@pytest.mark.django_db
def test_location_detail_includes_broader_coverage(client: Client) -> None:
    croatia = Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    europe = Location.objects.create(
        slug="europe",
        title="Europe",
        coverage_type=Location.COVERAGE_REGIONAL,
        covered_country_codes=["HR", "DE", "IT"],
    )
    asia = Location.objects.create(
        slug="asia",
        title="Asia",
        coverage_type=Location.COVERAGE_REGIONAL,
        covered_country_codes=["JP", "KR"],
    )
    world = Location.objects.create(
        slug="world",
        title="World",
        coverage_type=Location.COVERAGE_GLOBAL,
        covered_country_codes=["HR", "US", "JP"],
    )
    _create_package(external_id="hr", location=croatia, price="5.00")
    _create_package(external_id="eu", location=europe, price="20.00")
    _create_package(external_id="asia", location=asia, price="25.00")
    _create_package(external_id="world-pkg", location=world, price="50.00")

    response = client.get("/api/v1/locations/croatia/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == "croatia"
    assert payload["min_price_usd"] == "5.00"
    broader_slugs = {item["slug"] for item in payload["broader_locations"]}
    assert broader_slugs == {"europe", "world"}
    assert "asia" not in broader_slugs


@pytest.mark.django_db
def test_packages_list_filters_by_location(client: Client) -> None:
    croatia = Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    germany = Location.objects.create(
        slug="germany",
        title="Germany",
        country_code="DE",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    _create_package(external_id="hr", location=croatia, price="5.00")
    _create_package(external_id="de", location=germany, price="6.00")

    response = client.get("/api/v1/packages/?location=croatia")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == "hr"
