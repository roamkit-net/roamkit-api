"""Tests for Order product snapshot (purchase-time copy from Package)."""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.constants import LEDGER_CURRENCY
from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Location, Package
from apps.orders.models import Order
from apps.orders.product_snapshot import (
    backfill_order_product_snapshots,
    product_snapshot_kwargs,
)
from apps.orders.services.order_service import OrderService
from shared.providers.esim import OrderedSimDTO, OrderResult


class FakeOrderProvider:
    def __init__(self, result: OrderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls.append((package_id, customer_ref))
        return self.result


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="snap@example.com", password="secret123")


@pytest.fixture
def location(db) -> Location:
    return Location.objects.create(
        slug="croatia",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )


@pytest.fixture
def package(db, location: Location) -> Package:
    return Package.objects.create(
        external_id="cronet-7days-1gb",
        title="1 GB - 7 days",
        operator_title="Cronet",
        country_code="HR",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("4.00"),
        net_price_usd=Decimal("1.30"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def order_result(package: Package) -> OrderResult:
    return OrderResult(
        external_order_id="2210523",
        code="20260728-2210523",
        package_id=package.external_id,
        customer_ref="rk-test",
        currency="USD",
        price_usd=Decimal("1.30"),
        manual_installation="",
        qrcode_installation="",
        installation_guide_url="",
        sims=[
            OrderedSimDTO(
                iccid="8910300000063703418",
                lpa="consumer.e-sim.global",
                matching_id="TNTEST",
                qrcode="LPA:1$consumer.e-sim.global$TNTEST",
                qrcode_url="https://example.com/qr",
                direct_apple_installation_url="",
            )
        ],
    )


def _fund(user: User, amount: str = "20.00") -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="test-fund-snap",
        idempotency_key=f"fund-snap:{user.pk}:{amount}",
    )


@pytest.mark.django_db
def test_product_snapshot_kwargs_uses_ledger_currency(package: Package) -> None:
    snap = product_snapshot_kwargs(package)
    assert snap["currency"] == LEDGER_CURRENCY
    assert snap["retail_price_usd"] == Decimal("4.00")
    assert snap["net_price_usd"] == Decimal("1.30")
    assert snap["location_title"] == "Croatia"
    assert snap["country_code"] == "HR"
    assert snap["package_title"] == "1 GB - 7 days"
    assert snap["validity_days"] == 7
    assert snap["data_allowance"] == "1 GB"


@pytest.mark.django_db
def test_fulfill_writes_immutable_product_snapshot(
    user: User, package: Package, order_result: OrderResult
) -> None:
    _fund(user)
    order = OrderService(FakeOrderProvider(order_result)).fulfill(
        user=user,
        package=package,
        idempotency_key="snap-purchase-1",
    )

    assert order.package_title == "1 GB - 7 days"
    assert order.operator_title == "Cronet"
    assert order.location_title == "Croatia"
    assert order.country_code == "HR"
    assert order.data_allowance == "1 GB"
    assert order.validity_days == 7
    assert order.retail_price_usd == Decimal("4.00")
    assert order.currency == LEDGER_CURRENCY
    assert order.net_price_usd == Decimal("1.30")


@pytest.mark.django_db
def test_snapshot_unchanged_when_package_price_changes_after_purchase(
    user: User, package: Package, order_result: OrderResult
) -> None:
    _fund(user)
    order = OrderService(FakeOrderProvider(order_result)).fulfill(
        user=user,
        package=package,
        idempotency_key="snap-immutable-1",
    )
    assert order.retail_price_usd == Decimal("4.00")

    package.price_usd = Decimal("5.50")
    package.title = "1 GB - 7 days (updated)"
    package.net_price_usd = Decimal("2.00")
    package.save(update_fields=["price_usd", "title", "net_price_usd", "updated_at"])

    order.refresh_from_db()
    assert order.retail_price_usd == Decimal("4.00")
    assert order.package_title == "1 GB - 7 days"
    assert order.net_price_usd == Decimal("1.30")
    assert order.currency == LEDGER_CURRENCY


@pytest.mark.django_db
def test_backfill_fills_empty_snapshot_from_package(
    user: User, package: Package
) -> None:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    assert order.retail_price_usd is None
    assert order.package_title == ""

    updated = backfill_order_product_snapshots(Order)
    assert updated == 1

    order.refresh_from_db()
    assert order.retail_price_usd == Decimal("4.00")
    assert order.package_title == "1 GB - 7 days"
    assert order.location_title == "Croatia"
    assert order.currency == LEDGER_CURRENCY

    # Idempotent: already filled rows are skipped.
    assert backfill_order_product_snapshots(Order) == 0


@pytest.mark.django_db
def test_backfill_tolerates_inconsistent_package(
    user: User, package: Package, caplog: pytest.LogCaptureFixture
) -> None:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
    )

    broken = SimpleNamespace(
        title="x",
        # missing operator_title / price_usd / etc. → AttributeError in kwargs
    )

    class _BrokenOrder:
        pk = order.pk
        package = broken
        updated_at = order.updated_at

        def save(self, **kwargs):  # noqa: ANN003
            raise AssertionError("must not save broken snapshot")

    class _QS:
        def filter(self, **kwargs):  # noqa: ANN003
            return self

        def select_related(self, *args):  # noqa: ANN002
            return self

        def order_by(self, *args):  # noqa: ANN002
            return self

        def iterator(self):
            yield _BrokenOrder()

    class _OrderProxy:
        objects = _QS()

    with caplog.at_level(logging.WARNING):
        updated = backfill_order_product_snapshots(_OrderProxy)

    assert updated == 0
    assert any("inconsistent package" in r.message for r in caplog.records)

    order.refresh_from_db()
    assert order.retail_price_usd is None


@pytest.mark.django_db
def test_backfill_tolerates_missing_package(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from django.core.exceptions import ObjectDoesNotExist

    class _MissingPkgOrder:
        pk = 999001

        @property
        def package(self):
            raise ObjectDoesNotExist("package gone")

        def save(self, **kwargs):  # noqa: ANN003
            raise AssertionError("must not save")

    class _QS:
        def filter(self, **kwargs):  # noqa: ANN003
            return self

        def select_related(self, *args):  # noqa: ANN002
            return self

        def order_by(self, *args):  # noqa: ANN002
            return self

        def iterator(self):
            yield _MissingPkgOrder()

    class _OrderProxy:
        objects = _QS()

    with caplog.at_level(logging.WARNING):
        updated = backfill_order_product_snapshots(_OrderProxy)

    assert updated == 0
    assert any("package missing" in r.message for r in caplog.records)


@pytest.mark.django_db(transaction=True)
def test_order_product_snapshot_migration_backfill() -> None:
    """orders.0004 backfills snapshots for pre-existing orders."""
    executor = MigrationExecutor(connection)
    executor.migrate([("orders", "0003_order_idempotency_key")])
    executor.loader.build_graph()

    state = executor.loader.project_state(
        [
            ("accounts", "0002_billing_schema"),
            ("billing", "0001_billing_schema"),
            ("orders", "0003_order_idempotency_key"),
            ("catalog", "0005_package_activation_policy"),
        ]
    )
    UserHistorical = state.apps.get_model("accounts", "User")
    AccountHistorical = state.apps.get_model("billing", "Account")
    PackageHistorical = state.apps.get_model("catalog", "Package")
    LocationHistorical = state.apps.get_model("catalog", "Location")
    OrderHistorical = state.apps.get_model("orders", "Order")

    import uuid

    user = UserHistorical.objects.create(
        email="order-snap-mig@example.com",
        password="!",
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )
    account = AccountHistorical.objects.create(
        id=uuid.uuid4(),
        user_id=user.pk,
        balance=Decimal("0"),
        version=0,
    )
    location = LocationHistorical.objects.create(
        slug="croatia-mig",
        title="Croatia",
        country_code="HR",
        coverage_type="local",
        image_url="",
        covered_country_codes=[],
        coverages=[],
        is_popular=False,
    )
    package = PackageHistorical.objects.create(
        external_id="pkg-snap-mig",
        title="1 GB - 7 days",
        operator_title="Cronet",
        country_code="HR",
        location_id=location.pk,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("4.00"),
        net_price_usd=Decimal("1.30"),
        is_unlimited=False,
        plan_type="data",
        source="airalo",
        is_active=True,
        synced_at=timezone.now(),
        activation_policy="first_usage",
    )
    order = OrderHistorical.objects.create(
        account_id=account.pk,
        package_id=package.pk,
        status="fulfilled",
        external_order_id="2210523",
        customer_ref="rk-8",
    )

    executor.migrate([("orders", "0004_order_product_snapshot")])
    executor.loader.build_graph()

    OrderAfter = executor.loader.project_state(
        [("orders", "0004_order_product_snapshot")]
    ).apps.get_model("orders", "Order")
    migrated = OrderAfter.objects.get(pk=order.pk)
    assert migrated.retail_price_usd == Decimal("4.00")
    assert migrated.package_title == "1 GB - 7 days"
    assert migrated.location_title == "Croatia"
    assert migrated.country_code == "HR"
    assert migrated.currency == LEDGER_CURRENCY
    assert migrated.net_price_usd == Decimal("1.30")

    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
