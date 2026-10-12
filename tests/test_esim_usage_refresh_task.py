"""Tests for periodic Airalo usage refresh."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.esims.tasks import refresh_airalo_usage
from apps.orders.models import Order
from shared.providers.esim import UsageDTO


class FakeUsageProvider:
    def __init__(self, *, failing_iccids: set[str] | None = None) -> None:
        self.failing_iccids = failing_iccids or set()
        self.calls: list[str] = []

    def get_usage(self, iccid: str) -> UsageDTO:
        self.calls.append(iccid)
        if iccid in self.failing_iccids:
            raise RuntimeError("provider unavailable")
        return UsageDTO(
            remaining_mb=512,
            total_mb=1024,
            expired_at=None,
            is_unlimited=False,
            status="ACTIVE",
            remaining_voice=0,
            remaining_text=0,
            total_voice=0,
            total_text=0,
        )

    def list_sim_packages(self, iccid: str) -> list:
        return []


@pytest.fixture
def esim_factory(db):
    owner = User.objects.create_user(
        email="periodic-usage@example.com",
        password="SecurePass1!",
    )
    package = Package.objects.create(
        external_id="periodic-refresh-package",
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
    next_suffix = 0

    def create(
        *,
        status: str,
        usage_synced_at=None,
    ) -> Esim:
        nonlocal next_suffix
        next_suffix += 1
        return Esim.objects.create(
            account=owner.billing_account,
            user=owner,
            order=order,
            iccid=f"8985235032610030{next_suffix:04d}",
            status=status,
            usage_synced_at=usage_synced_at,
        )

    return create


@pytest.mark.django_db
def test_periodic_refresh_only_processes_stale_selected_statuses(
    esim_factory,
) -> None:
    purchased = esim_factory(status=Esim.Status.PURCHASED)
    in_use = esim_factory(
        status=Esim.Status.IN_USE,
        usage_synced_at=timezone.now() - timedelta(minutes=6),
    )
    unknown = esim_factory(status=Esim.Status.UNKNOWN)
    fresh = esim_factory(
        status=Esim.Status.PURCHASED,
        usage_synced_at=timezone.now() - timedelta(minutes=1),
    )
    installed = esim_factory(status=Esim.Status.INSTALLED)
    expired = esim_factory(status=Esim.Status.EXPIRED)
    provider = FakeUsageProvider()

    with patch(
        "shared.providers.factory.get_topup_provider",
        return_value=provider,
    ):
        stats = refresh_airalo_usage()

    assert stats == {"eligible": 3, "refreshed": 3, "failed": 0, "packages_failed": 0}
    assert provider.calls == [purchased.iccid, in_use.iccid, unknown.iccid]
    for refreshed in (purchased, in_use, unknown):
        refreshed.refresh_from_db()
        assert refreshed.usage_status == "ACTIVE"
        assert refreshed.status == Esim.Status.IN_USE
    for skipped in (fresh, installed, expired):
        skipped.refresh_from_db()
        assert skipped.usage_status == ""


@pytest.mark.django_db
def test_periodic_refresh_isolates_provider_failures(esim_factory) -> None:
    failing = esim_factory(status=Esim.Status.PURCHASED)
    succeeding = esim_factory(status=Esim.Status.PURCHASED)
    provider = FakeUsageProvider(failing_iccids={failing.iccid})

    with patch(
        "shared.providers.factory.get_topup_provider",
        return_value=provider,
    ):
        stats = refresh_airalo_usage()

    assert stats == {
        "eligible": 2,
        "refreshed": 1,
        "failed": 1,
        "packages_failed": 0,
    }
    assert provider.calls == [failing.iccid, succeeding.iccid]
    failing.refresh_from_db()
    succeeding.refresh_from_db()
    assert failing.usage_synced_at is None
    assert succeeding.usage_status == "ACTIVE"


@pytest.mark.django_db
@override_settings(AIRALO_ENABLED=False)
def test_periodic_refresh_does_nothing_when_airalo_is_disabled(
    esim_factory,
) -> None:
    esim_factory(status=Esim.Status.PURCHASED)

    with patch("shared.providers.factory.get_topup_provider") as get_provider:
        stats = refresh_airalo_usage()

    assert stats == {
        "eligible": 0,
        "refreshed": 0,
        "failed": 0,
        "packages_failed": 0,
    }
    get_provider.assert_not_called()


def test_periodic_refresh_schedule_is_five_minutes() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE["esims-refresh-airalo-usage"]
    assert schedule == {
        "task": "esims.refresh_airalo_usage",
        "schedule": 300.0,
    }
