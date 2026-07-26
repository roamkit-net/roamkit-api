"""Model and migration tests for apps.billing (PR2 schema)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.registration import register_user
from apps.billing.models import (
    Account,
    CreditLedgerEntry,
    DepositRequest,
    LedgerReferenceType,
    Subscription,
)
from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order


@pytest.mark.django_db
def test_create_user_yields_account_via_signal() -> None:
    user = User.objects.create_user(email="signal@example.com", password="secret123")
    account = Account.objects.get(user=user)
    assert account.balance == Decimal("0")
    assert account.version == 0
    assert isinstance(account.pk, uuid.UUID)


@pytest.mark.django_db
def test_register_user_yields_account() -> None:
    register_user(email="reg@example.com")
    user = User.objects.get(email="reg@example.com")
    assert Account.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_ensure_billing_account_is_idempotent() -> None:
    user = User.objects.create_user(email="ensure@example.com", password="secret123")
    first = ensure_billing_account(user)
    second = ensure_billing_account(user)
    assert first.pk == second.pk
    assert Account.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_billing_models_use_uuid_primary_keys() -> None:
    user = User.objects.create_user(email="uuid@example.com", password="secret123")
    account = user.billing_account
    deposit = DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        idempotency_key="dep-uuid-1",
    )
    ledger = CreditLedgerEntry.objects.create(
        account=account,
        delta=Decimal("10.000000"),
        balance_after=Decimal("10.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id=str(deposit.pk),
        idempotency_key="led-uuid-1",
    )
    package = Package.objects.create(
        external_id="pkg-uuid",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    esim = Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000009001",
        status=Esim.Status.PURCHASED,
    )
    subscription = Subscription.objects.create(
        account=account,
        esim=esim,
        price_per_period=Decimal("5.000000"),
        next_billing_date=date(2026, 8, 1),
    )

    for obj in (account, deposit, ledger, subscription):
        assert isinstance(obj.pk, uuid.UUID)


@pytest.mark.django_db
def test_money_fields_store_six_decimal_places() -> None:
    user = User.objects.create_user(email="money@example.com", password="secret123")
    account = user.billing_account
    amount = Decimal("1.123456")

    account.balance = amount
    account.save(update_fields=["balance", "updated_at"])
    account.refresh_from_db()
    assert account.balance == amount

    deposit = DepositRequest.objects.create(
        account=account,
        amount_requested=amount,
        amount_credited=amount,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        idempotency_key="money-dep-1",
        status=DepositRequest.Status.COMPLETED,
    )
    deposit.refresh_from_db()
    assert deposit.amount_requested == amount
    assert deposit.amount_credited == amount


@pytest.mark.django_db
def test_tx_hash_and_idempotency_key_unique() -> None:
    user = User.objects.create_user(email="uniq@example.com", password="secret123")
    account = user.billing_account
    DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("5.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash="0xabc123",
        idempotency_key="idem-1",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        DepositRequest.objects.create(
            account=account,
            amount_requested=Decimal("6.000000"),
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            tx_hash="0xabc123",
            idempotency_key="idem-2",
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        DepositRequest.objects.create(
            account=account,
            amount_requested=Decimal("6.000000"),
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            tx_hash="0xdef456",
            idempotency_key="idem-1",
        )

    CreditLedgerEntry.objects.create(
        account=account,
        delta=Decimal("1.000000"),
        balance_after=Decimal("1.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="adj-1",
        idempotency_key="ledger-idem-1",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        CreditLedgerEntry.objects.create(
            account=account,
            delta=Decimal("2.000000"),
            balance_after=Decimal("3.000000"),
            reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
            reference_id="adj-2",
            idempotency_key="ledger-idem-1",
        )


@pytest.mark.django_db
def test_check_constraints_reject_invalid_rows() -> None:
    user = User.objects.create_user(email="checks@example.com", password="secret123")
    account = user.billing_account

    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.filter(pk=account.pk).update(balance=Decimal("-0.000001"))

    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.filter(pk=account.pk).update(version=-1)

    with pytest.raises(IntegrityError), transaction.atomic():
        DepositRequest.objects.create(
            account=account,
            amount_requested=Decimal("0"),
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            idempotency_key="bad-amount",
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        DepositRequest.objects.create(
            account=account,
            amount_requested=Decimal("1.000000"),
            amount_credited=Decimal("0"),
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            idempotency_key="bad-credited",
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        CreditLedgerEntry.objects.create(
            account=account,
            delta=Decimal("0"),
            balance_after=Decimal("0"),
            reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
            reference_id="zero",
            idempotency_key="bad-delta",
        )

    package = Package.objects.create(
        external_id="pkg-check",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(account=account, package=package)
    esim = Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000009002",
        status=Esim.Status.PURCHASED,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Subscription.objects.create(
            account=account,
            esim=esim,
            price_per_period=Decimal("0"),
            next_billing_date=date(2026, 8, 1),
        )


@pytest.mark.django_db(transaction=True)
def test_account_backfill_migration() -> None:
    """Applying billing.0001 backfills Account for users that already existed."""
    executor = MigrationExecutor(connection)
    # Roll back billing + order account migration to pre-billing state.
    executor.migrate(
        [
            ("orders", "0001_initial"),
            ("billing", None),
        ]
    )
    executor.loader.build_graph()

    state = executor.loader.project_state([("accounts", "0002_billing_schema")])
    UserHistorical = state.apps.get_model("accounts", "User")
    user = UserHistorical.objects.create(
        email="pre-billing@example.com",
        password="!",
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )

    executor.migrate([("billing", "0001_billing_schema")])
    executor.loader.build_graph()

    AccountHistorical = executor.loader.project_state(
        [("billing", "0001_billing_schema")]
    ).apps.get_model("billing", "Account")
    assert AccountHistorical.objects.filter(user_id=user.pk).exists()
    account = AccountHistorical.objects.get(user_id=user.pk)
    assert account.balance == Decimal("0")
    assert account.version == 0

    # Restore to HEAD for subsequent tests in this process.
    executor.migrate(
        [
            ("billing", "0001_billing_schema"),
            ("orders", "0002_billing_schema"),
        ]
    )


@pytest.mark.django_db(transaction=True)
def test_order_account_backfill_migration() -> None:
    """orders.0002 backfills Order.account from the owner's billing Account."""
    executor = MigrationExecutor(connection)
    executor.migrate(
        [
            ("orders", "0001_initial"),
            ("billing", "0001_billing_schema"),
        ]
    )
    executor.loader.build_graph()

    state = executor.loader.project_state(
        [
            ("accounts", "0002_billing_schema"),
            ("billing", "0001_billing_schema"),
            ("orders", "0001_initial"),
            ("catalog", "0004_location_coverages"),
        ]
    )
    UserHistorical = state.apps.get_model("accounts", "User")
    AccountHistorical = state.apps.get_model("billing", "Account")
    PackageHistorical = state.apps.get_model("catalog", "Package")
    OrderHistorical = state.apps.get_model("orders", "Order")

    user = UserHistorical.objects.create(
        email="order-mig@example.com",
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
    package = PackageHistorical.objects.create(
        external_id="pkg-order-mig",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        is_unlimited=False,
        plan_type="data",
        source="airalo",
        is_active=True,
        synced_at=timezone.now(),
    )
    order = OrderHistorical.objects.create(
        user_id=user.pk,
        package_id=package.pk,
        status="fulfilled",
        external_order_id="",
        customer_ref="",
    )

    executor.migrate([("orders", "0002_billing_schema")])
    executor.loader.build_graph()

    OrderAfter = executor.loader.project_state(
        [("orders", "0002_billing_schema")]
    ).apps.get_model("orders", "Order")
    migrated = OrderAfter.objects.get(pk=order.pk)
    assert migrated.account_id == account.pk
    field_names = {f.name for f in OrderAfter._meta.fields}
    assert "account" in field_names
    assert "user" not in field_names
