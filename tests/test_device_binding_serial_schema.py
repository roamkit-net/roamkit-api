"""DeviceBinding.uem_serial_number schema constraints (ADR 021 Option C′)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.models import DeviceBindingStatus
from apps.organizations.services import create_device_binding, create_organization

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="bind-serial@example.com", password="x")


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-bind-serial",
        title="1 GB",
        operator_title="Change",
        country_code="HR",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("8.00"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Bind Serial Org", actor=owner)


def _esim(owner, org, package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=org.account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-6:]}",
        customer_ref=f"ref-{iccid[-6:]}",
    )
    return Esim.objects.create(
        user=owner,
        account=org.account,
        order=order,
        iccid=iccid,
        status=Esim.Status.INSTALLED,
    )


@pytest.mark.django_db
def test_active_bindings_unique_serial_per_org(owner, org, package):
    first = create_device_binding(
        owner, org.pk, esim_id=_esim(owner, org, package, "891000000000000001").pk
    ).binding
    first.uem_serial_number = "SERIAL-A"
    first.save(update_fields=["uem_serial_number", "updated_at"])

    second = create_device_binding(
        owner, org.pk, esim_id=_esim(owner, org, package, "891000000000000002").pk
    ).binding
    second.uem_serial_number = "SERIAL-A"
    with pytest.raises(IntegrityError):
        second.save(update_fields=["uem_serial_number", "updated_at"])


@pytest.mark.django_db
def test_empty_serial_allowed_on_multiple_active_pr18_bindings(owner, org, package):
    a = create_device_binding(
        owner, org.pk, esim_id=_esim(owner, org, package, "891000000000000011").pk
    ).binding
    b = create_device_binding(
        owner, org.pk, esim_id=_esim(owner, org, package, "891000000000000012").pk
    ).binding
    assert a.uem_serial_number == ""
    assert b.uem_serial_number == ""
    assert a.status == DeviceBindingStatus.ACTIVE
    assert b.status == DeviceBindingStatus.ACTIVE
