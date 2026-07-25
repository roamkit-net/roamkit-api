"""Tests for UsageService cache updates."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.esims.services.usage_service import UsageService
from apps.orders.models import Order
from shared.providers.esim import TopupPackage, UsageDTO


class FakeTopupProvider:
    def __init__(self, usage: UsageDTO) -> None:
        self.usage = usage

    def get_usage(self, iccid: str) -> UsageDTO:
        return self.usage

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return []

    def submit_topup(self, iccid: str, package_id: str) -> None:
        raise AssertionError("unused")


@pytest.fixture
def esim(db) -> Esim:
    user = User.objects.create_user(email="owner@example.com", password="secret123")
    package = Package.objects.create(
        external_id="pkg-1",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="1",
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000001111",
        status=Esim.Status.UNUSED,
    )


@pytest.mark.django_db
def test_usage_service_updates_cache(esim: Esim) -> None:
    usage = UsageDTO(
        remaining_mb=100,
        total_mb=1000,
        expired_at="2026-01-15 12:00:00",
        is_unlimited=False,
        status="ACTIVE",
        remaining_voice=1,
        remaining_text=2,
        total_voice=3,
        total_text=4,
    )
    service = UsageService(FakeTopupProvider(usage))

    result = service.get_usage(esim)

    assert result.remaining_mb == 100
    esim.refresh_from_db()
    assert esim.usage_remaining_mb == 100
    assert esim.usage_total_mb == 1000
    assert esim.usage_status == "ACTIVE"
    assert esim.usage_is_unlimited is False
    assert esim.usage_synced_at is not None
    assert esim.usage_expired_at is not None
    assert esim.usage_expired_at.year == 2026


@pytest.mark.django_db
def test_usage_service_handles_null_expired_at(esim: Esim) -> None:
    usage = UsageDTO(
        remaining_mb=0,
        total_mb=0,
        expired_at=None,
        is_unlimited=True,
        status="UNKNOWN",
        remaining_voice=0,
        remaining_text=0,
        total_voice=0,
        total_text=0,
    )
    UsageService(FakeTopupProvider(usage)).get_usage(esim)

    esim.refresh_from_db()
    assert esim.usage_expired_at is None
    assert esim.usage_is_unlimited is True
