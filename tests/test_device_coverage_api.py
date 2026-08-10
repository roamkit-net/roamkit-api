"""POST /api/v1/device/coverage/ — snapshot-only, same auth as status."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.product_snapshot import (
    backfill_order_product_snapshots,
    product_snapshot_kwargs,
)
from apps.organizations.services import create_device_binding, create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
SERIAL = "36281JEGR04531"
GUID = "fb3de589-14c1-4b95-a215-2b0c7d44199d"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="cov-owner@example.com", password=PASSWORD)


@pytest.fixture
def org(owner):
    return create_organization(name="Cov Fleet", actor=owner)


def _device_coverage(client, *, device_external_id: str, credential: str, **extra):
    body = {
        "device_external_id": device_external_id,
        "credential": credential,
        **extra,
    }
    return client.post(
        "/api/v1/device/coverage/",
        data=json.dumps(body),
        content_type="application/json",
    )


def _bind_with_order(owner, org, *, coverage_snapshot, coverage_type="regional"):
    suffix = uuid.uuid4().hex[:12]
    location = Location.objects.create(
        slug=f"loc-{coverage_type}-{suffix}",
        title="Europe",
        country_code="",
        coverage_type=coverage_type,
        coverages=[
            {
                "code": "HR",
                "name": "Live Croatia",
                "networks": [{"name": "LiveOp", "types": ["5G"]}],
            }
        ],
    )
    package = Package.objects.create(
        external_id=f"pkg-cov-{suffix}",
        title="Europe Regional",
        operator_title="Airalo",
        country_code="",
        location=location,
        data_allowance="5 GB",
        validity_days=30,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
        is_active=True,
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        package_title="Europe Regional",
        location_title="Europe",
        coverage_type=coverage_type,
        data_allowance="5 GB",
        validity_days=30,
        coverage_snapshot=coverage_snapshot,
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid=f"891000{suffix.zfill(12)[:12]}",
        status=Esim.Status.IN_USE,
    )
    issued = create_device_binding(owner, org.pk, esim_id=esim.pk)
    return issued.binding, issued.credential, order, location


@pytest.mark.django_db
def test_coverage_returns_snapshot_not_live_catalog(client, owner, org):
    snapshot = [
        {
            "country_code": "HR",
            "country_name": "Croatia",
            "operators": ["A1", "Telemach"],
        },
        {
            "country_code": "SI",
            "country_name": None,
            "operators": [],
        },
    ]
    binding, credential, order, location = _bind_with_order(
        owner, org, coverage_snapshot=snapshot
    )
    location.coverages = [
        {
            "code": "DE",
            "name": "Germany",
            "networks": [{"name": "Changed", "types": ["5G"]}],
        }
    ]
    location.save()

    resp = _device_coverage(
        client,
        device_external_id=binding.device_external_id,
        credential=credential,
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["device_external_id"] == binding.device_external_id
    assert body["coverage_type"] == "regional"
    assert body["coverage"] == snapshot
    blob = json.dumps(body)
    assert "networks" not in blob
    assert "types" not in blob
    assert "LiveOp" not in blob
    assert "Germany" not in blob
    # ORM row unchanged
    order.refresh_from_db()
    assert order.coverage_snapshot == snapshot


@pytest.mark.django_db
def test_coverage_legacy_null(client, owner, org):
    binding, credential, order, _location = _bind_with_order(
        owner, org, coverage_snapshot=None
    )
    assert order.coverage_snapshot is None
    resp = _device_coverage(
        client,
        device_external_id=binding.device_external_id,
        credential=credential,
    )
    assert resp.status_code == 200
    assert resp.json()["coverage"] is None


@pytest.mark.django_db
def test_credential_a_cannot_read_device_b_coverage(client, owner, org):
    snap_a = [
        {
            "country_code": "HR",
            "country_name": "Croatia",
            "operators": ["A1"],
        }
    ]
    snap_b = [
        {
            "country_code": "IT",
            "country_name": "Italy",
            "operators": ["TIM"],
        }
    ]
    binding_a, cred_a, _oa, _la = _bind_with_order(owner, org, coverage_snapshot=snap_a)
    binding_b, cred_b, _ob, _lb = _bind_with_order(owner, org, coverage_snapshot=snap_b)

    # Wrong credential for B's device id
    resp = _device_coverage(
        client,
        device_external_id=binding_b.device_external_id,
        credential=cred_a,
    )
    assert resp.status_code == 404

    # Correct pair only sees own snapshot
    ok = _device_coverage(
        client,
        device_external_id=binding_a.device_external_id,
        credential=cred_a,
    )
    assert ok.status_code == 200
    assert ok.json()["coverage"] == snap_a


@pytest.mark.django_db
def test_coverage_rejects_esim_id_and_org_fields(client, owner, org):
    binding, credential, _o, _l = _bind_with_order(
        owner,
        org,
        coverage_snapshot=[
            {"country_code": "HR", "country_name": "Croatia", "operators": []}
        ],
    )
    resp = _device_coverage(
        client,
        device_external_id=binding.device_external_id,
        credential=credential,
        esim_id=999,
    )
    assert resp.status_code == 400
    assert "esim_id" in resp.json()


@pytest.mark.django_db
def test_purchase_kwargs_write_normalized_snapshot(owner, org):
    location = Location.objects.create(
        slug="europe-norm",
        title="Europe",
        coverage_type=Location.COVERAGE_REGIONAL,
        coverages=[
            {
                "code": "si",
                "name": "Slovenia",
                "networks": [{"name": "b"}, {"name": "A"}, {"name": "a"}],
            },
            {"code": "USA", "name": "Bad", "networks": [{"name": "X"}]},
            {"code": "hr", "networks": []},
        ],
    )
    package = Package.objects.create(
        external_id="pkg-norm",
        title="EU",
        operator_title="X",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("1.00"),
        synced_at=timezone.now(),
    )
    snap = product_snapshot_kwargs(package)
    assert snap["coverage_snapshot"] == [
        {"country_code": "HR", "country_name": None, "operators": []},
        {"country_code": "SI", "country_name": "Slovenia", "operators": ["A", "b"]},
    ]


@pytest.mark.django_db
def test_backfill_does_not_set_coverage_snapshot_from_live_catalog(owner, org):
    location = Location.objects.create(
        slug="backfill-cov",
        title="Europe",
        coverage_type=Location.COVERAGE_GLOBAL,
        coverages=[
            {
                "code": "HR",
                "name": "Croatia",
                "networks": [{"name": "A1"}],
            }
        ],
    )
    package = Package.objects.create(
        external_id="pkg-backfill-cov",
        title="World",
        operator_title="X",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("2.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        # retail_price_usd null → eligible for product snapshot backfill
        coverage_snapshot=None,
    )
    updated = backfill_order_product_snapshots(Order)
    assert updated == 1
    order.refresh_from_db()
    assert order.coverage_snapshot is None
    assert order.package_title == "World"


def _serial_coverage(client, *, device_serial: str):
    return client.post(
        "/api/v1/device/coverage/",
        data=json.dumps({"device_serial": device_serial}),
        content_type="application/json",
    )


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_coverage_returns_snapshot_without_binding(client, owner, org):
    from apps.organizations.models import DeviceBinding

    snapshot = [
        {
            "country_code": "HR",
            "country_name": "Croatia",
            "operators": ["A1", "Telemach"],
        }
    ]
    suffix = uuid.uuid4().hex[:12]
    location = Location.objects.create(
        slug=f"loc-global-{suffix}",
        title="World",
        country_code="",
        coverage_type="global",
        coverages=[],
    )
    package = Package.objects.create(
        external_id=f"pkg-serial-cov-{suffix}",
        title="Global",
        operator_title="Airalo",
        country_code="",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
        is_active=True,
    )
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        package_title="Global",
        location_title="World",
        coverage_type="global",
        data_allowance="1 GB",
        validity_days=7,
        coverage_snapshot=snapshot,
    )
    iccid = f"891111{suffix.zfill(12)[:12]}"
    Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid=iccid,
        status=Esim.Status.IN_USE,
    )
    binding_count_before = DeviceBinding.objects.count()

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = {
            "guid": GUID,
            "serialNumber": SERIAL,
            "iccid": iccid,
            "sims": [{"iccid": iccid}],
        }
        resp = _serial_coverage(client, device_serial=SERIAL)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "device_external_id" in body
    assert body["device_external_id"] is None
    assert body["coverage_type"] == "global"
    assert body["coverage"] == snapshot
    assert DeviceBinding.objects.count() == binding_count_before


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_coverage_device_not_found(client, owner, org):
    from apps.integrations.blackberry_uem.client import BlackberryUemClientError

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.side_effect = (
            BlackberryUemClientError("UEM serialNumber match count is 0 (fail closed)")
        )
        resp = _serial_coverage(client, device_serial=SERIAL)
    assert resp.status_code == 404
    assert resp.json()["code"] == "device_not_found"


@pytest.mark.django_db
def test_coverage_rejects_mixed_pr18_and_serial(client, owner, org):
    binding, credential, _o, _l = _bind_with_order(
        owner,
        org,
        coverage_snapshot=[
            {"country_code": "HR", "country_name": "Croatia", "operators": []}
        ],
    )
    resp = client.post(
        "/api/v1/device/coverage/",
        data=json.dumps(
            {
                "device_external_id": binding.device_external_id,
                "credential": credential,
                "device_serial": SERIAL,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 400
