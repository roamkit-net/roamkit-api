"""Tests for balance reconcile / rebuild and metrics (PR8)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.billing.models import DepositRequest, LedgerReferenceType
from apps.billing.services import credit_service, rebuild_service, reconcile_service
from apps.billing.services.metrics import collect_billing_metrics
from apps.billing.tasks import reconcile_balances
from shared.events.billing_events import BalanceDriftDetected
from shared.events.event_bus import event_bus


@pytest.fixture
def account(db):
    user = User.objects.create_user(email="recon@example.com", password="secret123")
    return user.billing_account


@pytest.mark.django_db
def test_rebuild_restores_balance_from_ledger(account) -> None:
    credit_service.credit(
        account,
        Decimal("10.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="d1",
        idempotency_key="d1",
    )
    credit_service.debit(
        account,
        Decimal("3.000000"),
        reference_type=LedgerReferenceType.ORDER,
        reference_id="o1",
        idempotency_key="o1",
    )
    # Corrupt cache without going through CreditService (ops repair scenario).
    type(account).objects.filter(pk=account.pk).update(balance=Decimal("99.000000"))
    account.refresh_from_db()
    assert account.balance == Decimal("99.000000")

    after = credit_service.rebuild_balance_from_ledger(account)
    account.refresh_from_db()
    assert after == Decimal("7.000000")
    assert account.balance == Decimal("7.000000")


@pytest.mark.django_db
def test_reconcile_alerts_on_drift_without_mutating(account) -> None:
    credit_service.credit(
        account,
        Decimal("5.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="d2",
        idempotency_key="d2",
    )
    type(account).objects.filter(pk=account.pk).update(balance=Decimal("1.000000"))
    account.refresh_from_db()

    drifts: list[BalanceDriftDetected] = []
    event_bus.subscribe(BalanceDriftDetected, drifts.append)
    try:
        stats = reconcile_service.reconcile()
    finally:
        event_bus._handlers[BalanceDriftDetected].remove(drifts.append)

    account.refresh_from_db()
    assert stats["drifts"] == 1
    assert account.balance == Decimal("1.000000")  # unchanged
    assert len(drifts) == 1
    assert drifts[0].ledger_sum == Decimal("5.000000")
    assert drifts[0].cached_balance == Decimal("1.000000")


@pytest.mark.django_db
def test_rebuild_all_and_management_commands(account, capsys) -> None:
    credit_service.credit(
        account,
        Decimal("2.500000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="d3",
        idempotency_key="d3",
    )
    type(account).objects.filter(pk=account.pk).update(balance=Decimal("0"))
    stats = rebuild_service.rebuild_all()
    assert stats["repaired"] == 1

    call_command("billing_reconcile_balances")
    call_command("billing_rebuild_balances")
    call_command("billing_metrics")
    out = capsys.readouterr().out
    assert "drift" in out.lower() or "Checked" in out
    assert "total_deposited_usdt" in out or "total_spent" in out or "Checked" in out


@pytest.mark.django_db
def test_celery_reconcile_task(account) -> None:
    result = reconcile_balances()
    assert "checked" in result
    assert "drifts" in result


@pytest.mark.django_db
def test_metrics_aggregates_deposits_and_spend(account) -> None:
    credit_service.credit(
        account,
        Decimal("10.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-m",
        idempotency_key="dep-m",
    )
    DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("10.000000"),
        amount_credited=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash="0x" + "a" * 64,
        idempotency_key="dep-req-m",
        status=DepositRequest.Status.COMPLETED,
    )
    DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("1.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash="0x" + "b" * 64,
        idempotency_key="dep-fail-m",
        status=DepositRequest.Status.FAILED,
        failure_reason="bad",
    )
    credit_service.debit(
        account,
        Decimal("4.000000"),
        reference_type=LedgerReferenceType.ORDER,
        reference_id="ord-m",
        idempotency_key="ord-m",
    )

    metrics = collect_billing_metrics()
    assert metrics.deposit_count == 1
    assert metrics.total_deposited_usdt == Decimal("10.000000")
    assert metrics.average_deposit == Decimal("10.000000")
    assert metrics.total_spent_credits == Decimal("4.000000")
    assert metrics.failed_verify_count == 1
