"""POST /api/v1/device/packages/ — ADR 021 device package history."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.models import DeviceBinding
from apps.organizations.serializers import DeviceStatusRequestSerializer
from apps.organizations.services import create_device_binding, create_organization
from shared.providers.esim import SimPackageDTO

User = get_user_model()
PASSWORD = "SecurePass1!"
SERIAL = "36281JEGR04531"
GUID = "fb3de589-14c1-4b95-a215-2b0c7d44199d"
ICCID = "89852350326100304891"


class FakeSimPackageProvider:
    def __init__(
        self,
        rows: list[SimPackageDTO] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[str] = []

    def list_sim_packages(self, iccid: str) -> list[SimPackageDTO]:
        self.calls.append(iccid)
        if self.error is not None:
            raise self.error
        return self.rows


def _history_row(
    *,
    instance_id: str = "1",
    status: str = "active",
    plan_type: str = "sim",
    package_external_id: str = "pkg-us-1gb-7d",
    is_unlimited: bool = False,
    remaining_mb: int | None = 900,
    provider_order_id: str | None = None,
) -> SimPackageDTO:
    return SimPackageDTO(
        instance_id=instance_id,
        status=status,
        remaining_mb=remaining_mb,
        activated_at="2026-08-12T10:50:00+00:00",
        expired_at="2026-08-19T10:50:00+00:00",
        finished_at=None,
        package_external_id=package_external_id,
        plan_type=plan_type,
        data_allowance="Unlimited" if is_unlimited else "1 GB",
        validity_days=7,
        is_unlimited=is_unlimited,
        provider_order_id=provider_order_id,
    )


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        email="device-packages@example.com", password=PASSWORD
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Packages Org", actor=owner)


def _make_esim(*, account, user, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="airalo-order-1",
        **product_snapshot_kwargs(package),
    )
    return Esim.objects.create(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        status=Esim.Status.IN_USE,
    )


def _uem_device(*, guid=GUID, serial=SERIAL, iccid=ICCID):
    return {
        "guid": guid,
        "serialNumber": serial,
        "iccid": iccid,
        "sims": [{"iccid": iccid, "homeCarrier": "A1 HR"}],
    }


def _device_packages(client, payload: dict):
    return client.post(
        "/api/v1/device/packages/",
        data=json.dumps(payload),
        content_type="application/json",
    )


def _device_status(client, payload: dict):
    return client.post(
        "/api/v1/device/status/",
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_request_serializer_rejects_client_iccid():
    ser = DeviceStatusRequestSerializer(data={"device_serial": SERIAL, "iccid": ICCID})
    assert not ser.is_valid()
    assert "iccid" in ser.errors


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_packages_returns_iccid_and_local_paid_usd(
    client, owner, org, package, monkeypatch
):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    provider = FakeSimPackageProvider(
        [
            _history_row(instance_id="1", plan_type="sim", status="expired"),
            _history_row(
                instance_id="2",
                plan_type="topup",
                status="not_active",
                package_external_id="topup-1gb",
            ),
            _history_row(
                instance_id="3",
                plan_type="topup",
                status="unknown",
                package_external_id="topup-1gb",
            ),
        ]
    )
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    binding_count_before = DeviceBinding.objects.count()

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        resp = _device_packages(client, {"device_serial": SERIAL})

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["device_external_id"] is None
    assert body["iccid"] == ICCID
    assert [row["id"] for row in body["results"]] == ["1", "2", "3"]
    first = body["results"][0]
    assert first["kind"] == "esim"
    assert first["status"] == "expired"
    assert first["paid_usd"] == "11.50"
    assert first["currency"] == "USD"
    assert "price" not in first
    assert "net_price" not in first
    assert "net_price_usd" not in first
    assert body["results"][1]["status"] == "not_active"
    assert body["results"][2]["status"] == "unknown"
    assert provider.calls == [esim.iccid]
    assert DeviceBinding.objects.count() == binding_count_before
    blob = json.dumps(body)
    assert "net_price" not in blob


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_packages_rejects_client_iccid_without_provider_call(
    client, owner, org, package, monkeypatch
):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    provider = FakeSimPackageProvider([_history_row()])
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        resp = _device_packages(
            client, {"device_serial": SERIAL, "iccid": "891000000000000000"}
        )
    assert resp.status_code == 400
    assert provider.calls == []
    client_cls.assert_not_called()


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_packages_wrong_serial_is_404(client, owner, org, package, monkeypatch):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    provider = FakeSimPackageProvider([_history_row()])
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    from apps.integrations.blackberry_uem.client import BlackberryUemClientError

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.side_effect = (
            BlackberryUemClientError("UEM serialNumber match count is 0 (fail closed)")
        )
        resp = _device_packages(client, {"device_serial": SERIAL})
    assert resp.status_code == 404
    assert resp.json()["code"] == "device_not_found"
    assert provider.calls == []


@pytest.mark.django_db
def test_pr18_packages_uses_binding_esim(client, owner, org, package, monkeypatch):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    issued = create_device_binding(owner, org.pk, esim_id=esim.pk)
    provider = FakeSimPackageProvider([_history_row(is_unlimited=True, remaining_mb=0)])
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    resp = _device_packages(
        client,
        {
            "device_external_id": issued.binding.device_external_id,
            "credential": issued.credential,
        },
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["device_external_id"] == issued.binding.device_external_id
    assert body["iccid"] == ICCID
    row = body["results"][0]
    assert row["is_unlimited"] is True
    assert row["remaining_mb"] is None
    assert provider.calls == [ICCID]


@pytest.mark.django_db
def test_pr18_cannot_read_other_device_packages(
    client, owner, org, package, monkeypatch
):
    esim_a = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    esim_b = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000000002",
    )
    issued_a = create_device_binding(owner, org.pk, esim_id=esim_a.pk)
    issued_b = create_device_binding(owner, org.pk, esim_id=esim_b.pk)
    provider = FakeSimPackageProvider([_history_row()])
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    resp = _device_packages(
        client,
        {
            "device_external_id": issued_b.binding.device_external_id,
            "credential": issued_a.credential,
        },
    )
    assert resp.status_code == 404
    assert provider.calls == []


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_packages_provider_failure_leaves_status_usable(
    client, owner, org, package, monkeypatch
):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    provider = FakeSimPackageProvider(error=RuntimeError("airalo down"))
    monkeypatch.setattr(
        "shared.providers.factory.get_sim_package_provider",
        lambda: provider,
    )
    payload = {"device_serial": SERIAL}
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        packages = _device_packages(client, payload)
        status_resp = _device_status(client, payload)

    assert packages.status_code == 503
    assert packages.json()["code"] == "provider_unavailable"
    assert status_resp.status_code == 200
    assert status_resp.json()["esim"]["iccid"] == ICCID


@pytest.mark.django_db
@override_settings(ORGANIZATIONS_ENABLED=False)
def test_packages_hidden_when_organizations_disabled(client):
    resp = _device_packages(client, {"device_serial": SERIAL})
    assert resp.status_code == 404
