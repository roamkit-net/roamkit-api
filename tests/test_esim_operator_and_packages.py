"""Operator snapshot, sequence-0 initial credit, and Airalo package storage."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAppliedPackage, Topup
from apps.esims.services.applied_package_sync import sync_applied_packages
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from shared.providers.esim import SimPackageDTO


def _package(*, external_id: str, operator: str, price: str) -> Package:
    return Package.objects.create(
        external_id=external_id,
        title="300 MB - 3 days",
        operator_title=operator,
        country_code="",
        data_allowance="300 MB",
        validity_days=3,
        price_usd=Decimal(price),
        synced_at=timezone.now(),
    )


def _order(
    user: User,
    package: Package,
    *,
    operator: str,
    retail: str,
    net: str | None = None,
) -> Order:
    return Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        operator_title=operator,
        package_title=package.title,
        retail_price_usd=Decimal(retail),
        net_price_usd=Decimal(net) if net is not None else None,
        package_external_id="",
    )


@pytest.mark.django_db
def test_create_purchased_copies_operator_and_initial_credit_without_debit() -> None:
    user = User.objects.create_user(email="op-owner@example.com", password="x")
    package = _package(
        external_id="discover-in-3days-300mb",
        operator="Discover",
        price="1.00",
    )
    order = _order(user, package, operator="Discover", retail="1.00", net="0.70")

    esim = lifecycle_service.create_purchased(
        user=user,
        order=order,
        iccid="89852350326100300998",
    )

    esim.refresh_from_db()
    assert esim.operator_title == "Discover"
    initial = Topup.objects.get(esim=esim, sequence=0)
    assert initial.package_external_id == "discover-in-3days-300mb"
    assert initial.amount == Decimal("1.000000")
    assert initial.status == Topup.Status.FULFILLED
    assert initial.idempotency_key == f"initial-credit:{esim.pk}"
    assert initial.net_price_usd == Decimal("0.700000")
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.TOPUP
    ).exists()


@pytest.mark.django_db
def test_migration_backfill_numbers_initial_credit_as_zero() -> None:
    user = User.objects.create_user(email="backfill-owner@example.com", password="x")
    package = _package(
        external_id="discover-in-3days-300mb",
        operator="Discover",
        price="1.00",
    )
    order = _order(user, package, operator="Discover", retail="1.00", net="0.70")
    esim = Esim.objects.create(
        account=user.billing_account,
        user=user,
        order=order,
        iccid="89852350326100300999",
        operator_title="",
    )
    later = Topup.objects.create(
        account=user.billing_account,
        esim=esim,
        package_external_id="discover-in-30days-200mb-topup",
        amount=Decimal("2.34"),
        status=Topup.Status.FULFILLED,
        idempotency_key="later-topup",
    )
    Topup.objects.filter(pk=later.pk).update(sequence=None)

    migration = importlib.import_module(
        "apps.esims.migrations.0013_esim_operator_and_packages"
    )
    migration.copy_operator_and_number_topups(apps, None)

    esim.refresh_from_db()
    later.refresh_from_db()
    initial = Topup.objects.get(esim=esim, sequence=0)
    assert esim.operator_title == "Discover"
    assert initial.package_external_id == "discover-in-3days-300mb"
    assert initial.idempotency_key == f"initial-credit:{esim.pk}"
    assert initial.net_price_usd == Decimal("0.700000")
    assert later.sequence == 1
    assert later.amount == Decimal("2.340000")


class _PackageProvider:
    def __init__(self, rows: list[SimPackageDTO] | None = None, *, fail: bool = False):
        self.rows = rows or []
        self.fail = fail
        self.calls: list[str] = []

    def list_sim_packages(self, iccid: str) -> list[SimPackageDTO]:
        self.calls.append(iccid)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.rows


def _row(**kwargs) -> SimPackageDTO:
    defaults = {
        "instance_id": "1",
        "status": "active",
        "remaining_mb": 293,
        "activated_at": "2026-10-11T17:59:00+00:00",
        "expired_at": "2026-10-14T17:59:00+00:00",
        "finished_at": None,
        "package_external_id": "discover-in-3days-300mb",
        "plan_type": "sim",
        "data_allowance": "300 MB",
        "validity_days": 3,
        "is_unlimited": False,
        "amount_mb": 300,
        "provider_price_usd": Decimal("1.00"),
    }
    defaults.update(kwargs)
    return SimPackageDTO(**defaults)


@pytest.mark.django_db
def test_package_sync_writes_status_and_keeps_snapshot_on_failure() -> None:
    user = User.objects.create_user(email="sync-owner@example.com", password="x")
    package = _package(
        external_id="discover-in-3days-300mb",
        operator="Discover",
        price="1.00",
    )
    order = _order(user, package, operator="Discover", retail="1.00")
    esim = lifecycle_service.create_purchased(
        user=user,
        order=order,
        iccid="89852350326100300998",
    )
    later = Topup.objects.create(
        account=user.billing_account,
        esim=esim,
        package_external_id="discover-in-30days-200mb-topup",
        amount=Decimal("2.34"),
        status=Topup.Status.FULFILLED,
        idempotency_key="sync-later",
    )
    provider = _PackageProvider(
        [
            _row(),
            _row(
                instance_id="2",
                status="not_active",
                remaining_mb=200,
                expired_at="2026-11-13T16:59:00+00:00",
                package_external_id="discover-in-30days-200mb-topup",
                plan_type="topup",
                data_allowance="200 MB",
                validity_days=30,
                amount_mb=200,
                provider_price_usd=Decimal("1.35"),
            ),
        ]
    )

    assert sync_applied_packages(esim, provider) == 2

    initial = Topup.objects.get(esim=esim, sequence=0)
    later.refresh_from_db()
    assert initial.provider_package_status == Topup.ProviderPackageStatus.ACTIVE
    assert later.sequence == 1
    assert later.provider_package_status == Topup.ProviderPackageStatus.NOT_ACTIVE
    assert later.amount == Decimal("2.340000")
    stored = EsimAppliedPackage.objects.get(topup=later)
    assert stored.provider_price_usd == Decimal("1.35")
    assert stored.data_allowance == "200 MB"

    with pytest.raises(RuntimeError):
        sync_applied_packages(esim, _PackageProvider(fail=True))
    assert EsimAppliedPackage.objects.filter(esim=esim).count() == 2
    later.refresh_from_db()
    assert later.provider_package_status == Topup.ProviderPackageStatus.NOT_ACTIVE


@pytest.mark.django_db
def test_admin_shows_operator_and_topup_package_without_calling_airalo() -> None:
    staff = User.objects.create_superuser(
        email="pkg-admin@example.com",
        password="SecurePass1!",
    )
    owner = User.objects.create_user(email="pkg-owner@example.com", password="x")
    package = _package(
        external_id="discover-in-3days-300mb",
        operator="Discover",
        price="1.00",
    )
    other_package = _package(
        external_id="eurolink-7days-1gb",
        operator="Eurolink",
        price="5.00",
    )
    discover_order = _order(owner, package, operator="Discover", retail="1.00")
    other_order = _order(owner, other_package, operator="Eurolink", retail="5.00")
    discover = Esim.objects.create(
        account=owner.billing_account,
        user=owner,
        order=discover_order,
        iccid="89852350326100300998",
        operator_title="Discover",
    )
    Esim.objects.create(
        account=owner.billing_account,
        user=owner,
        order=other_order,
        iccid="89852350326100300002",
        operator_title="Eurolink",
    )
    topup = Topup.objects.create(
        account=owner.billing_account,
        esim=discover,
        package_external_id="discover-in-30days-200mb-topup",
        amount=Decimal("2.34"),
        status=Topup.Status.FULFILLED,
        provider_package_status=Topup.ProviderPackageStatus.NOT_ACTIVE,
        idempotency_key="admin-later",
    )
    EsimAppliedPackage.objects.create(
        esim=discover,
        topup=topup,
        instance_id="2",
        sequence=topup.sequence,
        package_external_id=topup.package_external_id,
        kind="topup",
        status="not_active",
        data_allowance="200 MB",
        validity_days=30,
        amount_mb=200,
        remaining_mb=200,
        provider_price_usd=Decimal("1.35"),
        synced_at=timezone.now(),
    )
    EsimAppliedPackage.objects.create(
        esim=discover,
        instance_id="1",
        sequence=0,
        package_external_id="discover-in-3days-300mb",
        kind="esim",
        status="active",
        data_allowance="300 MB",
        validity_days=3,
        amount_mb=300,
        remaining_mb=293,
        provider_price_usd=Decimal("1.00"),
        synced_at=timezone.now(),
    )

    client = Client()
    client.force_login(staff)
    listed = client.get("/admin/esims/esim/")
    assert listed.status_code == 200
    assert b"Discover" in listed.content
    filtered = client.get("/admin/esims/esim/", {"operator_title": "Discover"})
    assert filtered.status_code == 200
    assert b"89852350326100300998" in filtered.content
    assert b"89852350326100300002" not in filtered.content

    change = client.get(f"/admin/esims/esim/{discover.pk}/change/")
    assert change.status_code == 200
    assert b"Discover" in change.content
    assert b"Data left: 493 MB" in change.content
    assert b"200 MB" in change.content

    topup_page = client.get(f"/admin/esims/topup/{topup.pk}/change/")
    assert topup_page.status_code == 200
    assert b"Not active" in topup_page.content
    assert b"$1.35 USD" in topup_page.content
    assert "200 MB · 30 days" in topup_page.content.decode()


@pytest.mark.django_db
def test_admin_shows_paid_and_wholesale_without_calling_airalo() -> None:
    staff = User.objects.create_superuser(
        email="price-admin@example.com",
        password="SecurePass1!",
    )
    owner = User.objects.create_user(email="price-owner@example.com", password="x")
    package = _package(
        external_id="discover-in-3days-300mb",
        operator="Discover",
        price="1.00",
    )
    order = _order(owner, package, operator="Discover", retail="1.00", net="0.70")
    esim = Esim.objects.create(
        account=owner.billing_account,
        user=owner,
        order=order,
        iccid="89852350326100300998",
        operator_title="Discover",
    )
    initial = Topup.objects.create(
        account=owner.billing_account,
        esim=esim,
        package_external_id="discover-in-3days-300mb",
        amount=Decimal("1.00"),
        status=Topup.Status.FULFILLED,
        sequence=0,
        idempotency_key=f"initial-credit:{esim.pk}",
    )
    later = Topup.objects.create(
        account=owner.billing_account,
        esim=esim,
        package_external_id="discover-in-30days-200mb-topup",
        amount=Decimal("2.34"),
        net_price_usd=Decimal("0.90"),
        status=Topup.Status.FULFILLED,
        provider_package_status=Topup.ProviderPackageStatus.NOT_ACTIVE,
        idempotency_key="discounted-later",
    )
    EsimAppliedPackage.objects.create(
        esim=esim,
        topup=initial,
        instance_id="1",
        sequence=0,
        package_external_id=initial.package_external_id,
        kind="esim",
        status="active",
        data_allowance="300 MB",
        validity_days=3,
        amount_mb=300,
        remaining_mb=293,
        provider_price_usd=Decimal("1.00"),
        synced_at=timezone.now(),
    )
    EsimAppliedPackage.objects.create(
        esim=esim,
        topup=later,
        instance_id="2",
        sequence=later.sequence,
        package_external_id=later.package_external_id,
        kind="topup",
        status="not_active",
        data_allowance="200 MB",
        validity_days=30,
        amount_mb=200,
        remaining_mb=200,
        provider_price_usd=Decimal("1.35"),
        synced_at=timezone.now(),
    )

    client = Client()
    client.force_login(staff)
    change = client.get(f"/admin/esims/esim/{esim.pk}/change/")
    assert change.status_code == 200
    page = change.content.decode()
    assert "Retail price" in page
    assert "$1.00 USD" in page
    assert "$0.70 USD" in page
    assert "$2.34 USD" in page
    assert "$0.90 USD" in page
    assert "1.35" in page

    topup_page = client.get(f"/admin/esims/topup/{later.pk}/change/")
    assert topup_page.status_code == 200
    later_page = topup_page.content.decode()
    assert "$1.35 USD" in later_page
    assert "$2.34 USD" in later_page
    assert "$0.90 USD" in later_page
    assert "$0.70 USD" not in later_page
