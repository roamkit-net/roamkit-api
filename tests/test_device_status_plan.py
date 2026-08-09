"""Device status ``plan`` field — Order snapshot only (no live catalog)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.services import create_device_binding, create_organization
from apps.organizations.services.device_status import _plan_snapshot

User = get_user_model()
PASSWORD = "SecurePass1!"
STATUS_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "apps"
    / "organizations"
    / "services"
    / "device_status.py"
)


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="plan-owner@example.com", password=PASSWORD)


@pytest.fixture
def org(owner):
    return create_organization(name="Plan Fleet", actor=owner)


def _access_token(client: Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["access"]


def _auth(client, user):
    return {"HTTP_AUTHORIZATION": f"Bearer {_access_token(client, user.email)}"}


@pytest.mark.django_db
def test_plan_full_order_snapshot(client, owner, org):
    location = Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    package = Package.objects.create(
        external_id="pkg-cronet-unl-3d",
        title="Cronet (Croatia)",
        operator_title="Cronet",
        country_code="HR",
        location=location,
        data_allowance="Unlimited",
        validity_days=3,
        price_usd=Decimal("5.00"),
        is_unlimited=True,
        synced_at=timezone.now(),
        is_active=True,
    )
    snap = product_snapshot_kwargs(package)
    assert snap["coverage_type"] == Location.COVERAGE_LOCAL
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-plan-full",
        **snap,
    )
    # Live catalog later changes must not affect status plan.
    location.coverage_type = Location.COVERAGE_GLOBAL
    location.title = "Changed Live"
    location.save()
    package.title = "Changed Live Package"
    package.save()

    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="891000000000100001",
        status=Esim.Status.IN_USE,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk).binding

    resp = client.get(
        f"/api/v1/orgs/{org.pk}/devices/{binding.device_external_id}/status/",
        **_auth(client, owner),
    )
    assert resp.status_code == 200, resp.content
    plan = resp.json()["plan"]
    assert plan == {
        "title": "Cronet (Croatia)",
        "data_allowance": "Unlimited",
        "validity_days": 3,
        "country_code": "HR",
        "coverage_type": "local",
        "location_title": "Croatia",
        # Local + empty coverages → snapshotted [] → not available for Coverage UI.
        "coverage_summary": {"available": False, "country_count": 0},
    }


@pytest.mark.django_db
def test_plan_legacy_partial_snapshot_null_coverage(client, owner, org):
    package = Package.objects.create(
        external_id="pkg-partial",
        title="Discover",
        operator_title="Airalo",
        country_code="",
        data_allowance="300 MB",
        validity_days=3,
        price_usd=Decimal("9.00"),
        synced_at=timezone.now(),
        is_active=True,
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-partial",
        package_title="Discover",
        data_allowance="300 MB",
        validity_days=3,
        # legacy: no country_code / coverage_type / location_title
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="891000000000100002",
        status=Esim.Status.ACTIVATED,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk).binding

    resp = client.get(
        f"/api/v1/orgs/{org.pk}/devices/{binding.device_external_id}/status/",
        **_auth(client, owner),
    )
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    assert plan["title"] == "Discover"
    assert plan["data_allowance"] == "300 MB"
    assert plan["validity_days"] == 3
    assert plan["country_code"] is None
    assert plan["coverage_type"] is None
    assert plan["location_title"] is None
    assert plan["coverage_summary"] is None


@pytest.mark.django_db
def test_plan_null_when_order_has_no_snapshot_fields(
    client,
    owner,
    org,
):
    package = Package.objects.create(
        external_id="pkg-empty-snap",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
        is_active=True,
    )
    # Order created without copying product snapshot (legacy empty row).
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-empty",
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="891000000000100003",
        status=Esim.Status.INSTALLED,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk).binding

    resp = client.get(
        f"/api/v1/orgs/{org.pk}/devices/{binding.device_external_id}/status/",
        **_auth(client, owner),
    )
    assert resp.status_code == 200
    assert resp.json()["plan"] is None


def test_plan_snapshot_helper_never_uses_live_package_location():
    source = STATUS_SERVICE.read_text(encoding="utf-8")
    # Guardrail: plan builder must stay snapshot-only (ignore docstring text).
    assert "def _plan_snapshot" in source
    plan_fn = source.split("def _plan_snapshot", 1)[1].split("\ndef ", 1)[0]
    body = plan_fn.split('"""', 2)[-1]
    assert "package.location" not in body
    assert "package__location" not in body
    assert "order.package" not in body
    assert "select_related" not in body


@pytest.mark.django_db
def test_plan_snapshot_unit_prefers_package_title(owner, org):
    package = Package.objects.create(
        external_id="pkg-unit",
        title="Live Title Ignored",
        operator_title="X",
        country_code="DE",
        data_allowance="5 GB",
        validity_days=30,
        price_usd=Decimal("1.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        package_title="Eurolink",
        location_title="Europe",
        country_code="",
        coverage_type="regional",
        data_allowance="5 GB",
        validity_days=30,
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="891000000000100099",
        status=Esim.Status.IN_USE,
    )
    plan = _plan_snapshot(esim)
    assert plan is not None
    assert plan["title"] == "Eurolink"
    assert plan["coverage_type"] == "regional"
    assert plan["country_code"] is None
    assert plan["coverage_summary"] is None


@pytest.mark.django_db
def test_coverage_summary_counts_normalized_list_and_available_for_regional(
    client, owner, org
):
    location = Location.objects.create(
        slug="eu-summary",
        title="Europe",
        coverage_type=Location.COVERAGE_REGIONAL,
        coverages=[],
    )
    package = Package.objects.create(
        external_id="pkg-summary",
        title="EU",
        operator_title="X",
        location=location,
        data_allowance="5 GB",
        validity_days=30,
        price_usd=Decimal("1.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        package_title="EU",
        coverage_type="regional",
        data_allowance="5 GB",
        validity_days=30,
        coverage_snapshot=[
            {"country_code": "HR", "country_name": "Croatia", "operators": ["A1"]},
            {"country_code": "SI", "country_name": "Slovenia", "operators": []},
        ],
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="891000000000100088",
        status=Esim.Status.IN_USE,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk).binding
    resp = client.get(
        f"/api/v1/orgs/{org.pk}/devices/{binding.device_external_id}/status/",
        **_auth(client, owner),
    )
    assert resp.status_code == 200
    summary = resp.json()["plan"]["coverage_summary"]
    assert summary == {"available": True, "country_count": 2}
    # Status must not embed the full list.
    assert "operators" not in json.dumps(resp.json()["plan"])
