"""Service tests for CreditService (sole money mutator)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.exceptions import (
    CreditServiceError,
    InsufficientFundsError,
    InvalidAmountError,
    InvalidReferenceTypeError,
)
from apps.billing.models import (
    REFERENCE_MODELS,
    Account,
    CreditLedgerEntry,
    LedgerReferenceType,
)
from apps.billing.services import CreditService, credit_service, resolve_reference
from apps.catalog.models import Package
from apps.orders.models import Order


@pytest.fixture
def account(db) -> Account:
    user = User.objects.create_user(email="credit@example.com", password="secret123")
    return user.billing_account


@pytest.mark.django_db
def test_credit_increments_balance_and_version(account: Account) -> None:
    entry = credit_service.credit(
        account,
        Decimal("10.500000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-1",
        idempotency_key="credit-1",
    )
    account.refresh_from_db()

    assert entry.delta == Decimal("10.500000")
    assert entry.balance_after == Decimal("10.500000")
    assert entry.reference_type == LedgerReferenceType.DEPOSIT
    assert account.balance == Decimal("10.500000")
    assert account.version == 1
    assert CreditLedgerEntry.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_debit_decrements_balance(account: Account) -> None:
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-2",
        idempotency_key="credit-2",
    )
    entry = credit_service.debit(
        account,
        Decimal("7.250000"),
        reference_type=LedgerReferenceType.ORDER,
        reference_id="ord-1",
        idempotency_key="debit-1",
    )
    account.refresh_from_db()

    assert entry.delta == Decimal("-7.250000")
    assert entry.balance_after == Decimal("12.750000")
    assert account.balance == Decimal("12.750000")
    assert account.version == 2


@pytest.mark.django_db
def test_debit_insufficient_funds_raises(account: Account) -> None:
    with pytest.raises(InsufficientFundsError):
        credit_service.debit(
            account,
            Decimal("1.000000"),
            reference_type=LedgerReferenceType.ORDER,
            reference_id="ord-2",
            idempotency_key="debit-under",
        )
    account.refresh_from_db()
    assert account.balance == Decimal("0")
    assert account.version == 0
    assert CreditLedgerEntry.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_idempotent_credit_returns_same_entry(account: Account) -> None:
    first = credit_service.credit(
        account,
        Decimal("5.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-3",
        idempotency_key="same-key",
    )
    second = credit_service.credit(
        account,
        Decimal("5.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-3",
        idempotency_key="same-key",
    )
    account.refresh_from_db()

    assert first.pk == second.pk
    assert account.balance == Decimal("5.000000")
    assert account.version == 1
    assert CreditLedgerEntry.objects.filter(idempotency_key="same-key").count() == 1


@pytest.mark.django_db
def test_admin_adjustment_and_refund_via_credit_service(account: Account) -> None:
    credit_service.credit(
        account,
        Decimal("10.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="ticket-1",
        idempotency_key="adj-1",
    )
    credit_service.debit(
        account,
        Decimal("3.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="ticket-1",
        idempotency_key="adj-2",
    )
    credit_service.credit(
        account,
        Decimal("3.000000"),
        reference_type=LedgerReferenceType.REFUND,
        reference_id="ord-refund",
        idempotency_key="refund-1",
    )
    account.refresh_from_db()
    assert account.balance == Decimal("10.000000")
    assert account.version == 3


@pytest.mark.django_db
def test_invalid_amount_and_reference_type(account: Account) -> None:
    with pytest.raises(InvalidAmountError):
        credit_service.credit(
            account,
            Decimal("0"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id="x",
            idempotency_key="bad-amt",
        )
    with pytest.raises(InvalidAmountError):
        credit_service.credit(
            account,
            Decimal("-1"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id="x",
            idempotency_key="bad-amt-2",
        )
    with pytest.raises(InvalidReferenceTypeError):
        credit_service.credit(
            account,
            Decimal("1"),
            reference_type="not_a_type",
            reference_id="x",
            idempotency_key="bad-ref",
        )
    with pytest.raises(CreditServiceError):
        credit_service.credit(
            account,
            Decimal("1"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id="x",
            idempotency_key="",
        )


@pytest.mark.django_db
def test_money_quantized_to_six_decimal_places(account: Account) -> None:
    entry = credit_service.credit(
        account,
        Decimal("1.1234567"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-q",
        idempotency_key="quant-1",
    )
    assert entry.delta == Decimal("1.123457")
    account.refresh_from_db()
    assert account.balance == Decimal("1.123457")


@pytest.mark.django_db(transaction=True)
def test_concurrent_credits_serialize_with_select_for_update(account: Account) -> None:
    """Two concurrent credits must both apply; version ends at 2."""

    def _credit(key: str) -> None:
        # Fresh DB connection per thread (Django default).
        CreditService().credit(
            account,
            Decimal("1.000000"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id=key,
            idempotency_key=key,
        )
        connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_credit, f"concurrent-{i}") for i in range(2)]
        for fut in futures:
            fut.result()

    account.refresh_from_db()
    assert account.balance == Decimal("2.000000")
    assert account.version == 2
    assert CreditLedgerEntry.objects.filter(account=account).count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_idempotency_key_creates_one_entry(account: Account) -> None:
    def _credit() -> str:
        entry = CreditService().credit(
            account,
            Decimal("4.000000"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id="dep-race",
            idempotency_key="race-key",
        )
        connection.close()
        return str(entry.pk)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _credit(), range(2)))

    assert results[0] == results[1]
    account.refresh_from_db()
    assert account.balance == Decimal("4.000000")
    assert account.version == 1
    assert CreditLedgerEntry.objects.filter(idempotency_key="race-key").count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_debits_contend_for_same_balance(account: Account) -> None:
    """Two parallel full-balance debits: exactly one wins, balance stays >= 0."""
    CreditService().credit(
        account,
        Decimal("10.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-contend",
        idempotency_key="dep-contend",
    )

    def _debit(key: str) -> str:
        try:
            CreditService().debit(
                account,
                Decimal("10.000000"),
                reference_type=LedgerReferenceType.ORDER,
                reference_id=key,
                idempotency_key=key,
            )
            connection.close()
            return "ok"
        except InsufficientFundsError:
            connection.close()
            return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: _debit(f"debit-contend-{i}"), range(2)))

    assert sorted(results) == ["insufficient", "ok"]
    account.refresh_from_db()
    assert account.balance == Decimal("0.000000")
    assert account.version == 2
    debits = CreditLedgerEntry.objects.filter(
        account=account, delta=Decimal("-10.000000")
    )
    assert debits.count() == 1


@pytest.mark.django_db
def test_reference_models_registry_resolves_order(account: Account) -> None:
    assert REFERENCE_MODELS[LedgerReferenceType.ORDER] is Order
    assert REFERENCE_MODELS[LedgerReferenceType.DEPOSIT] is not None
    assert REFERENCE_MODELS[LedgerReferenceType.SUBSCRIPTION] is not None

    package = Package.objects.create(
        external_id="pkg-ref",
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
        status=Order.Status.PENDING_PAYMENT,
    )
    resolved = resolve_reference(LedgerReferenceType.ORDER, str(order.pk))
    assert resolved is not None
    assert resolved.pk == order.pk
    assert resolve_reference(LedgerReferenceType.TOPUP, "anything") is None


@pytest.mark.django_db
def test_credit_uses_select_for_update(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure the money path locks the Account row."""
    calls: list[str] = []
    original = Account.objects.select_for_update

    def _tracking_select_for_update(*args, **kwargs):
        calls.append("select_for_update")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        Account.objects, "select_for_update", _tracking_select_for_update
    )

    with transaction.atomic():
        credit_service.credit(
            account,
            Decimal("1.000000"),
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id="lock-1",
            idempotency_key="lock-1",
        )

    assert calls == ["select_for_update"]
