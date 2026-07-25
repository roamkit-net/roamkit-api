"""Tests for billing admin money actions (PR8)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, override_settings

from apps.accounts.models import User
from apps.billing.admin import AccountAdmin
from apps.billing.exceptions import CreditServiceError
from apps.billing.models import (
    Account,
    CreditLedgerEntry,
    DepositRequest,
    LedgerReferenceType,
)
from apps.billing.services import credit_service, resolve_reference
from apps.billing.services.deposit_verification import DepositVerificationService
from shared.providers.blockchain import TransferResult


@pytest.fixture
def staff_user(db) -> User:
    user = User.objects.create_superuser(
        email="admin@example.com", password="secret123"
    )
    return user


@pytest.fixture
def account(db) -> Account:
    user = User.objects.create_user(email="adj@example.com", password="secret123")
    return user.billing_account


@pytest.mark.django_db
def test_admin_adjust_credits_via_credit_service(account, staff_user) -> None:
    entry = credit_service.admin_adjust(
        account,
        Decimal("12.500000"),
        credit=True,
        reason="goodwill",
        actor_id=staff_user.pk,
        idempotency_key="admin-adj-1",
    )
    account.refresh_from_db()
    assert entry.reference_type == LedgerReferenceType.ADMIN_ADJUSTMENT
    assert entry.delta == Decimal("12.500000")
    assert account.balance == Decimal("12.500000")
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_admin_adjust_requires_reason(account, staff_user) -> None:
    with pytest.raises(CreditServiceError):
        credit_service.admin_adjust(
            account,
            Decimal("1.000000"),
            credit=True,
            reason="  ",
            actor_id=staff_user.pk,
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_reverify_retries_failed_deposit(account) -> None:
    tx = "0x" + "c" * 64
    deposit = DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("5.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash=tx,
        idempotency_key="reverify-1",
        status=DepositRequest.Status.FAILED,
        failure_reason="temporary",
    )
    transfer = TransferResult(
        tx_hash=tx,
        status="success",
        amount=Decimal("5.000000"),
        confirmations=30,
        from_address="0xfrom",
        to_address="0xto",
        block_number=1,
        token_contract="0xtoken",
        raw_rpc_response={"ok": True},
    )
    provider = MagicMock()
    provider.fetch_usdt_transfer.return_value = transfer
    service = DepositVerificationService(
        blockchain_provider=provider, min_confirmations=1
    )

    result = service.reverify(deposit)
    assert result.status == DepositRequest.Status.COMPLETED
    account.refresh_from_db()
    assert account.balance == Decimal("5.000000")


@pytest.mark.django_db
def test_resolve_reference_deposit(account) -> None:
    deposit = DepositRequest.objects.create(
        account=account,
        amount_requested=Decimal("1.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash="0x" + "d" * 64,
        idempotency_key="ref-1",
        status=DepositRequest.Status.PENDING,
    )
    related = resolve_reference(LedgerReferenceType.DEPOSIT, str(deposit.pk))
    assert related == deposit


@pytest.mark.django_db
def test_account_admin_rebuild_action(account, staff_user) -> None:
    credit_service.credit(
        account,
        Decimal("3.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="rb",
        idempotency_key="rb",
    )
    Account.objects.filter(pk=account.pk).update(balance=Decimal("0"))
    site = AdminSite()
    admin_obj = AccountAdmin(Account, site)
    request = RequestFactory().post("/admin/")
    request.user = staff_user
    admin_obj.message_user = MagicMock()
    admin_obj.rebuild_selected_balances(request, Account.objects.filter(pk=account.pk))
    account.refresh_from_db()
    assert account.balance == Decimal("3.000000")
    admin_obj.message_user.assert_called_once()
