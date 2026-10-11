"""Admin tests for explicit Airalo usage refresh."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.admin.models import CHANGE, LogEntry
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from shared.providers.esim import UsageDTO


class FakeUsageProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def get_usage(self, iccid: str) -> UsageDTO:
        self.calls.append(iccid)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return UsageDTO(
            remaining_mb=512,
            total_mb=1024,
            expired_at="2026-11-01 12:00:00",
            is_unlimited=False,
            status="ACTIVE",
            remaining_voice=0,
            remaining_text=0,
            total_voice=0,
            total_text=0,
        )


@pytest.fixture
def staff(db) -> User:
    return User.objects.create_superuser(
        email="usage-admin@example.com",
        password="SecurePass1!",
    )


@pytest.fixture
def esim(db) -> Esim:
    owner = User.objects.create_user(
        email="usage-owner@example.com",
        password="SecurePass1!",
    )
    package = Package.objects.create(
        external_id="admin-refresh-package",
        title="1 GB",
        operator_title="Operator",
        country_code="HR",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=owner.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    return Esim.objects.create(
        account=owner.billing_account,
        user=owner,
        order=order,
        iccid="89852350326100300998",
        status=Esim.Status.PURCHASED,
    )


@pytest.mark.django_db
def test_admin_refresh_is_csrf_protected_and_updates_usage(
    staff: User,
    esim: Esim,
) -> None:
    provider = FakeUsageProvider()
    client = Client(enforce_csrf_checks=True)
    client.force_login(staff)
    change_url = f"/admin/esims/esim/{esim.pk}/change/"
    refresh_url = f"/admin/esims/esim/{esim.pk}/refresh-usage/"

    change_page = client.get(change_url)
    assert change_page.status_code == 200
    assert b"Refresh from Airalo" in change_page.content
    assert f'action="{refresh_url}"'.encode() in change_page.content

    rejected = client.post(refresh_url)
    assert rejected.status_code == 403
    assert provider.calls == []

    csrf_token = client.cookies["csrftoken"].value
    with patch("apps.esims.admin.get_topup_provider", return_value=provider):
        response = client.post(
            refresh_url,
            HTTP_X_CSRFTOKEN=csrf_token,
        )

    assert response.status_code == 302
    assert provider.calls == [esim.iccid]
    esim.refresh_from_db()
    assert esim.usage_status == "ACTIVE"
    assert esim.usage_remaining_mb == 512
    assert esim.usage_total_mb == 1024
    assert esim.usage_synced_at is not None
    assert esim.status == Esim.Status.IN_USE
    assert LogEntry.objects.filter(
        object_id=str(esim.pk),
        action_flag=CHANGE,
        change_message__contains="Refreshed Airalo usage",
    ).exists()


@pytest.mark.django_db
def test_admin_refresh_get_does_not_call_provider(
    staff: User,
    esim: Esim,
) -> None:
    provider = FakeUsageProvider()
    client = Client()
    client.force_login(staff)

    with patch("apps.esims.admin.get_topup_provider", return_value=provider):
        response = client.get(f"/admin/esims/esim/{esim.pk}/refresh-usage/")

    assert response.status_code == 405
    assert provider.calls == []
    esim.refresh_from_db()
    assert esim.usage_synced_at is None


@pytest.mark.django_db
def test_admin_refresh_provider_failure_keeps_existing_cache(
    staff: User,
    esim: Esim,
) -> None:
    esim.usage_status = "NOT_ACTIVE"
    esim.usage_remaining_mb = 900
    esim.usage_total_mb = 1024
    esim.usage_synced_at = timezone.now()
    esim.save(
        update_fields=[
            "usage_status",
            "usage_remaining_mb",
            "usage_total_mb",
            "usage_synced_at",
        ]
    )
    synced_at = esim.usage_synced_at
    provider = FakeUsageProvider(fail=True)
    client = Client()
    client.force_login(staff)

    with patch("apps.esims.admin.get_topup_provider", return_value=provider):
        response = client.post(f"/admin/esims/esim/{esim.pk}/refresh-usage/")

    assert response.status_code == 302
    esim.refresh_from_db()
    assert esim.usage_status == "NOT_ACTIVE"
    assert esim.usage_remaining_mb == 900
    assert esim.usage_total_mb == 1024
    assert esim.usage_synced_at == synced_at


@pytest.mark.django_db
def test_admin_refresh_requires_change_permission(esim: Esim) -> None:
    limited = User.objects.create_user(
        email="limited-usage-admin@example.com",
        password="SecurePass1!",
        is_staff=True,
    )
    client = Client()
    client.force_login(limited)

    response = client.post(f"/admin/esims/esim/{esim.pk}/refresh-usage/")

    assert response.status_code == 403
    esim.refresh_from_db()
    assert esim.usage_synced_at is None
