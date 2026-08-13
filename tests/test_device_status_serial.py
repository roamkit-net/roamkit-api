"""Serial-only device status shape (ADR 021 Option C″ — no DeviceBinding gate)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.integrations.blackberry_uem.client import BlackberryUemClientError
from apps.orders.models import Order
from apps.organizations.models import DeviceBinding
from apps.organizations.serializers import DeviceStatusRequestSerializer
from apps.organizations.services import (
    create_device_binding,
    create_organization,
)

User = get_user_model()
PASSWORD = "SecurePass1!"
SERIAL = "36281JEGR04531"
GUID = "fb3de589-14c1-4b95-a215-2b0c7d44199d"
ICCID = "89852350326100304891"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        email="serial-status@example.com", password=PASSWORD
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-serial-status",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="HR",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Serial Status Org", actor=owner)


def _make_esim(*, account, user, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-6:]}",
        customer_ref=f"ref-{iccid[-6:]}",
    )
    return Esim.objects.create(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        status=Esim.Status.INSTALLED,
        usage_is_unlimited=True,
        usage_expired_at=timezone.now() + timedelta(days=7),
    )


def _serial_status(client, *, device_serial):
    return client.post(
        "/api/v1/device/status/",
        data=json.dumps({"device_serial": device_serial}),
        content_type="application/json",
    )


def _uem_device(*, guid=GUID, serial=SERIAL, iccid=ICCID):
    return {
        "guid": guid,
        "serialNumber": serial,
        "iccid": iccid,
        "sims": [{"iccid": iccid, "homeCarrier": "A1 HR"}],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"device_external_id": "x"},
        {"credential": "y"},
        {"device_serial": ""},
        {"device_serial": "   "},
        {
            "device_external_id": "x",
            "credential": "y",
            "device_serial": SERIAL,
        },
        {
            "fleet_external_id": "f",
            "fleet_credential": "s",
            "device_serial": SERIAL,
        },
        {"fleet_external_id": "f", "fleet_credential": "s"},
        {},
    ],
)
def test_serializer_rejects_incomplete_mixed_or_fleet_shapes(payload):
    ser = DeviceStatusRequestSerializer(data=payload)
    assert not ser.is_valid()


def test_serializer_accepts_pr18_and_serial_shapes():
    pr18 = DeviceStatusRequestSerializer(
        data={"device_external_id": "dev-1", "credential": "secret"}
    )
    assert pr18.is_valid(), pr18.errors
    assert pr18.validated_data["auth_shape"] == "pr18"

    serial = DeviceStatusRequestSerializer(data={"device_serial": SERIAL})
    assert serial.is_valid(), serial.errors
    assert serial.validated_data["auth_shape"] == "serial"
    assert serial.validated_data["device_serial"] == SERIAL


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_without_binding_returns_null_external_id(
    client, owner, org, package
):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    binding_count_before = DeviceBinding.objects.count()

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        resp = _serial_status(client, device_serial=SERIAL)

    assert resp.status_code == 200, resp.content
    payload = resp.json()
    assert "device_external_id" in payload
    assert payload["device_external_id"] is None
    assert payload["binding_status"] is None
    assert payload["esim"]["iccid"] == ICCID
    assert payload["esim"]["id"] == esim.pk
    assert DeviceBinding.objects.count() == binding_count_before


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_ignores_stale_binding_when_uem_misses(
    client, owner, org, package
):
    """Stale DeviceBinding must not rescue a UEM failure (no cache fallback)."""
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    issued = create_device_binding(owner, org.id, esim_id=esim.pk)
    binding = issued.binding
    binding.uem_serial_number = SERIAL
    binding.uem_device_guid = GUID
    binding.save(update_fields=["uem_serial_number", "uem_device_guid", "updated_at"])
    updated_at = binding.updated_at

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.side_effect = (
            BlackberryUemClientError("UEM serialNumber match count is 0 (fail closed)")
        )
        resp = _serial_status(client, device_serial=SERIAL)

    assert resp.status_code == 404
    assert resp.json()["code"] == "device_not_found"
    binding.refresh_from_db()
    assert binding.uem_device_guid == GUID
    assert binding.updated_at == updated_at


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_device_not_found(client, owner, org, package):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.side_effect = (
            BlackberryUemClientError("UEM serialNumber match count is 0 (fail closed)")
        )
        resp = _serial_status(client, device_serial=SERIAL)
    assert resp.status_code == 404
    assert resp.json()["code"] == "device_not_found"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_device_ambiguous(client, owner, org, package):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.side_effect = (
            BlackberryUemClientError("UEM serialNumber match count is 2 (fail closed)")
        )
        resp = _serial_status(client, device_serial=SERIAL)
    assert resp.status_code == 404
    assert resp.json()["code"] == "device_ambiguous"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_iccid_not_found(client, owner, org, package):
    _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="8900000000000000999",
    )
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device(
            iccid=ICCID
        )
        resp = _serial_status(client, device_serial=SERIAL)
    assert resp.status_code == 404
    assert resp.json()["code"] == "iccid_not_found"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_resolves_personal_account_esim(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    personal = ensure_billing_account(owner)
    esim.account = personal
    esim.save(update_fields=["account", "updated_at"])

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        resp = _serial_status(client, device_serial=SERIAL)

    assert resp.status_code == 200, resp.content
    assert resp.json()["esim"]["id"] == esim.pk
    assert resp.json()["device_external_id"] is None
    esim.refresh_from_db()
    assert esim.account_id == personal.id


@pytest.mark.django_db
def test_esim_for_iccid_ambiguous_fails_closed(owner, org, package):
    """DB unique(iccid) normally prevents this; resolver must still fail closed."""
    from apps.organizations.exceptions import IccidAmbiguousError
    from apps.organizations.services.device_status import _esim_for_iccid

    first = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    second = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="89852350326100304892",
    )
    with patch(
        "apps.organizations.services.device_status.Esim.objects.select_related"
    ) as select_related:
        select_related.return_value.filter.return_value = [first, second]
        with pytest.raises(IccidAmbiguousError):
            _esim_for_iccid(ICCID)


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_iccid_ambiguous_http_code(client, owner, org, package):
    from apps.organizations.exceptions import IccidAmbiguousError

    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        with patch(
            "apps.organizations.services.device_status._esim_for_iccid",
            side_effect=IccidAmbiguousError("Multiple RoamKit eSIMs match this ICCID."),
        ):
            resp = _serial_status(client, device_serial=SERIAL)

    assert resp.status_code == 404
    assert resp.json()["code"] == "iccid_ambiguous"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_skips_archived_esim(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    esim.archived_at = timezone.now()
    esim.save(update_fields=["archived_at", "updated_at"])

    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = _uem_device()
        resp = _serial_status(client, device_serial=SERIAL)

    assert resp.status_code == 404
    assert resp.json()["code"] == "iccid_not_found"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_serial_status_uem_inventory_unavailable_without_iccid(
    client, owner, org, package
):
    _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    with patch(
        "apps.organizations.services.uem_serial.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_serial.return_value = {
            "guid": GUID,
            "serialNumber": SERIAL,
            "iccid": None,
            "sims": [],
        }
        resp = _serial_status(client, device_serial=SERIAL)
    assert resp.status_code == 503
    assert resp.json()["code"] == "uem_inventory_unavailable"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_pr18_status_still_works_unchanged(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    issued = create_device_binding(owner, org.id, esim_id=esim.pk)
    resp = client.post(
        "/api/v1/device/status/",
        data=json.dumps(
            {
                "device_external_id": issued.binding.device_external_id,
                "credential": issued.credential,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["esim"]["iccid"] == ICCID
    assert resp.json()["device_external_id"] == issued.binding.device_external_id


@pytest.mark.django_db
def test_fleet_fields_http_400(client):
    resp = client.post(
        "/api/v1/device/status/",
        data=json.dumps(
            {
                "fleet_external_id": "fleet-1",
                "fleet_credential": "secret",
                "device_serial": SERIAL,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_mixed_pr18_serial_http_400(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package, iccid=ICCID)
    issued = create_device_binding(owner, org.id, esim_id=esim.pk)
    resp = client.post(
        "/api/v1/device/status/",
        data=json.dumps(
            {
                "device_external_id": issued.binding.device_external_id,
                "credential": issued.credential,
                "device_serial": SERIAL,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 400
