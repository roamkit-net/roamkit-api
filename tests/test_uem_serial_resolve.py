"""UEM serialNumber unique-match resolver (ADR 021 Option C′)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.integrations.blackberry_uem.client import BlackberryUemClientError
from apps.orders.models import Order
from apps.organizations.exceptions import UemSerialMatchError
from apps.organizations.services import (
    create_device_binding,
    create_organization,
    refresh_binding_uem_guid_from_serial,
    resolve_uem_device_by_serial,
)

User = get_user_model()
SERIAL = "36281JEGR04531"
GUID_OLD = "bc473029-90d8-4476-bb79-3ac6eb17725d"
GUID_NEW = "fb3de589-14c1-4b95-a215-2b0c7d44199d"


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="serial-owner@example.com", password="x")


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-serial-1",
        title="1 GB",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("9.00"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Serial Org", actor=owner)


def _make_binding(owner, org, package, *, serial: str, guid: str = ""):
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-serial",
        customer_ref="ref-serial",
    )
    esim = Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid="89852350326100304891",
        status=Esim.Status.INSTALLED,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk).binding
    binding.uem_serial_number = serial
    binding.uem_device_guid = guid
    binding.save(update_fields=["uem_serial_number", "uem_device_guid", "updated_at"])
    return binding


@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_resolve_requires_exactly_one_match():
    client = MagicMock()
    client.get_device_by_serial.return_value = {
        "guid": GUID_NEW,
        "serialNumber": SERIAL,
        "iccid": "89852350326100304891",
    }
    device = resolve_uem_device_by_serial(SERIAL, client=client)
    assert device["guid"] == GUID_NEW
    client.get_device_by_serial.assert_called_once_with(SERIAL)


@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_resolve_fail_closed_on_zero_or_many():
    client = MagicMock()
    client.get_device_by_serial.side_effect = BlackberryUemClientError(
        "UEM serialNumber match count is 0 (fail closed)"
    )
    with pytest.raises(UemSerialMatchError):
        resolve_uem_device_by_serial(SERIAL, client=client)

    client.get_device_by_serial.side_effect = BlackberryUemClientError(
        "UEM serialNumber match count is 2 (fail closed)"
    )
    with pytest.raises(UemSerialMatchError):
        resolve_uem_device_by_serial(SERIAL, client=client)


@override_settings(BLACKBERRY_UEM_ENABLED=False)
def test_resolve_disabled_fail_closed():
    with pytest.raises(UemSerialMatchError):
        resolve_uem_device_by_serial(SERIAL, client=MagicMock())


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_refresh_guid_only_on_unique_success(owner, org, package):
    binding = _make_binding(owner, org, package, serial=SERIAL, guid=GUID_OLD)
    client = MagicMock()
    client.get_device_by_serial.return_value = {
        "guid": GUID_NEW,
        "serialNumber": SERIAL,
        "iccid": "89852350326100304891",
        "sims": [{"iccid": "89852350326100304891"}],
    }

    device = refresh_binding_uem_guid_from_serial(binding, client=client)
    binding.refresh_from_db()
    assert device["guid"] == GUID_NEW
    assert binding.uem_device_guid == GUID_NEW


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_refresh_does_not_update_guid_when_match_fails(owner, org, package):
    binding = _make_binding(owner, org, package, serial=SERIAL, guid=GUID_OLD)
    client = MagicMock()
    client.get_device_by_serial.side_effect = BlackberryUemClientError(
        "UEM serialNumber match count is 2 (fail closed)"
    )
    with pytest.raises(UemSerialMatchError):
        refresh_binding_uem_guid_from_serial(binding, client=client)
    binding.refresh_from_db()
    assert binding.uem_device_guid == GUID_OLD


def test_client_get_device_by_serial_enforces_unique_count():
    from apps.integrations.blackberry_uem.client import BlackberryUemClient

    client = BlackberryUemClient.__new__(BlackberryUemClient)
    client.list_devices = MagicMock(
        return_value=[
            {"guid": "a", "serialNumber": SERIAL},
            {"guid": "b", "serialNumber": SERIAL},
        ]
    )
    with pytest.raises(BlackberryUemClientError, match="fail closed"):
        BlackberryUemClient.get_device_by_serial(client, SERIAL)

    client.list_devices = MagicMock(
        return_value=[{"guid": "a", "serialNumber": "other"}]
    )
    with pytest.raises(BlackberryUemClientError, match="fail closed"):
        BlackberryUemClient.get_device_by_serial(client, SERIAL)

    client.list_devices = MagicMock(
        return_value=[{"guid": GUID_NEW, "serialNumber": SERIAL}]
    )
    device = BlackberryUemClient.get_device_by_serial(client, SERIAL)
    assert device["guid"] == GUID_NEW
