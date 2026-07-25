"""Tests for DepositVerificationService (PR4)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings

from apps.accounts.models import User
from apps.billing.exceptions import (
    BillingDisabledError,
    DepositVerificationError,
    DepositVerificationFailedError,
    DuplicateTransactionError,
    InsufficientConfirmationsError,
)
from apps.billing.models import (
    Account,
    CreditLedgerEntry,
    DepositRequest,
    LedgerReferenceType,
)
from apps.billing.services.deposit_verification import DepositVerificationService
from shared.events.billing_events import CreditGranted, DepositVerified
from shared.events.event_bus import event_bus
from shared.providers.blockchain import (
    BlockchainProviderError,
    BlockchainRPCError,
    TransferNotFoundError,
    TransferResult,
)

TX_HASH = "0x" + ("ab" * 32)
TX_HASH_OTHER = "0x" + ("cd" * 32)


class _FakeBlockchainProvider:
    def __init__(self, result: TransferResult | Exception) -> None:
        self._result = result
        self.calls: list[str] = []

    def fetch_usdt_transfer(self, tx_hash: str) -> TransferResult:
        self.calls.append(tx_hash)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _transfer(
    *,
    amount: Decimal = Decimal("10.000000"),
    confirmations: int = 30,
    status: str = "success",
    tx_hash: str = TX_HASH,
    raw: dict[str, Any] | None = None,
) -> TransferResult:
    return TransferResult(
        tx_hash=tx_hash,
        from_address="0x1111111111111111111111111111111111111111",
        to_address="0x2222222222222222222222222222222222222222",
        amount=amount,
        confirmations=confirmations,
        block_number=100,
        token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        status=status,
        raw_rpc_response=raw or {"receipt": {"status": "0x1"}, "tx_hash": tx_hash},
    )


@pytest.fixture
def account(db) -> Account:
    user = User.objects.create_user(email="deposit@example.com", password="secret123")
    return user.billing_account


@pytest.fixture
def collected_events():
    received: list[object] = []

    def _capture_deposit(event: DepositVerified) -> None:
        received.append(event)

    def _capture_credit(event: CreditGranted) -> None:
        received.append(event)

    event_bus.subscribe(DepositVerified, _capture_deposit)
    event_bus.subscribe(CreditGranted, _capture_credit)
    try:
        yield received
    finally:
        # EventBus has no unsubscribe; clear handlers for isolation in this process.
        event_bus._handlers[DepositVerified].remove(_capture_deposit)
        event_bus._handlers[CreditGranted].remove(_capture_credit)


def _service(
    provider: _FakeBlockchainProvider, **kwargs: Any
) -> DepositVerificationService:
    return DepositVerificationService(
        blockchain_provider=provider,  # type: ignore[arg-type]
        min_confirmations=kwargs.pop("min_confirmations", 20),
        **kwargs,
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_success_credits_account_and_publishes_snapshots(
    account: Account, collected_events: list[object]
) -> None:
    raw = {"receipt": {"status": "0x1"}, "matched": True}
    provider = _FakeBlockchainProvider(_transfer(amount=Decimal("15.500000"), raw=raw))
    service = _service(provider)

    deposit = service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("15.500000"),
        idempotency_key="dep-success-1",
    )

    account.refresh_from_db()
    deposit.refresh_from_db()

    assert deposit.status == DepositRequest.Status.COMPLETED
    assert deposit.amount_credited == Decimal("15.500000")
    assert deposit.verified_at is not None
    assert deposit.raw_rpc_response == raw
    assert account.balance == Decimal("15.500000")
    assert account.version == 1
    assert CreditLedgerEntry.objects.filter(account=account).count() == 1
    entry = CreditLedgerEntry.objects.get(account=account)
    assert entry.reference_type == LedgerReferenceType.DEPOSIT
    assert entry.reference_id == str(deposit.pk)
    assert entry.idempotency_key == f"deposit:{deposit.pk}"

    assert len(collected_events) == 2
    verified = collected_events[0]
    granted = collected_events[1]
    assert isinstance(verified, DepositVerified)
    assert isinstance(granted, CreditGranted)
    assert verified.event_version == 1
    assert verified.deposit_id == str(deposit.pk)
    assert verified.account_id == str(account.pk)
    assert verified.amount == Decimal("15.500000")
    assert verified.balance_after == Decimal("15.500000")
    assert verified.tx_hash == TX_HASH
    assert verified.payment_method == DepositRequest.PaymentMethod.WALLET_CONNECT
    assert verified.ledger_entry_id == str(entry.pk)
    assert verified.verified_at == deposit.verified_at
    assert granted.event_version == 1
    assert granted.account_id == str(account.pk)
    assert granted.amount == Decimal("15.500000")
    assert granted.balance_after == Decimal("15.500000")
    assert granted.reference_type == LedgerReferenceType.DEPOSIT
    assert granted.reference_id == str(deposit.pk)
    assert granted.ledger_entry_id == str(entry.pk)
    assert granted.created_at == entry.created_at


@pytest.mark.django_db(transaction=True)
@override_settings(BILLING_ENABLED=True)
def test_concurrent_verify_same_idempotency_key_credits_once(account: Account) -> None:
    """Two parallel verify calls: one credit, one ledger row, same deposit."""
    provider = _FakeBlockchainProvider(_transfer(amount=Decimal("10.000000")))
    account_id = account.pk

    def _verify() -> str:
        # Fresh Account reference + DB connection per thread (Django default).
        acct = Account.objects.get(pk=account_id)
        service = DepositVerificationService(
            blockchain_provider=provider,  # type: ignore[arg-type]
            min_confirmations=20,
        )
        deposit = service.verify(
            acct,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="race-verify",
        )
        connection.close()
        return str(deposit.pk)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _verify(), range(2)))

    assert results[0] == results[1]
    account.refresh_from_db()
    assert account.balance == Decimal("10.000000")
    assert account.version == 1
    assert DepositRequest.objects.filter(idempotency_key="race-verify").count() == 1
    assert (
        DepositRequest.objects.get(idempotency_key="race-verify").status
        == DepositRequest.Status.COMPLETED
    )
    assert CreditLedgerEntry.objects.filter(account=account).count() == 1
    assert (
        CreditLedgerEntry.objects.filter(idempotency_key__startswith="deposit:").count()
        == 1
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_idempotent_on_idempotency_key(
    account: Account, collected_events: list[object]
) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)

    first = service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        amount_requested=Decimal("10.000000"),
        idempotency_key="same-key",
    )
    second = service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        amount_requested=Decimal("10.000000"),
        idempotency_key="same-key",
    )

    account.refresh_from_db()
    assert first.pk == second.pk
    assert second.status == DepositRequest.Status.COMPLETED
    assert account.balance == Decimal("10.000000")
    assert CreditLedgerEntry.objects.filter(account=account).count() == 1
    assert len(provider.calls) == 1
    assert len(collected_events) == 2  # events only on first success


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rejects_duplicate_completed_tx_hash(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)
    service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="first-key",
    )

    other_user = User.objects.create_user(
        email="other-deposit@example.com", password="secret123"
    )
    with pytest.raises(DuplicateTransactionError):
        service.verify(
            other_user.billing_account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            amount_requested=Decimal("10.000000"),
            idempotency_key="second-key",
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_under_confirmed_keeps_pending(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer(confirmations=5))
    service = _service(provider, min_confirmations=20)

    with pytest.raises(InsufficientConfirmationsError) as exc_info:
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="pending-conf",
        )

    assert exc_info.value.confirmations == 5
    assert exc_info.value.required == 20
    deposit = DepositRequest.objects.get(idempotency_key="pending-conf")
    assert deposit.status == DepositRequest.Status.PENDING
    assert deposit.raw_rpc_response is not None
    account.refresh_from_db()
    assert account.balance == Decimal("0")
    assert CreditLedgerEntry.objects.count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_under_confirmed_can_retry_when_confirmed(
    account: Account, collected_events: list[object]
) -> None:
    provider = _FakeBlockchainProvider(_transfer(confirmations=5))
    service = _service(provider, min_confirmations=20)

    with pytest.raises(InsufficientConfirmationsError):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="retry-conf",
        )

    provider._result = _transfer(confirmations=25)
    deposit = service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="retry-conf",
    )

    assert deposit.status == DepositRequest.Status.COMPLETED
    account.refresh_from_db()
    assert account.balance == Decimal("10.000000")
    assert len(collected_events) == 2


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_amount_mismatch_marks_failed(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer(amount=Decimal("9.000000")))
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError, match="Amount mismatch"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
            amount_requested=Decimal("10.000000"),
            idempotency_key="amt-mismatch",
        )

    deposit = DepositRequest.objects.get(idempotency_key="amt-mismatch")
    assert deposit.status == DepositRequest.Status.FAILED
    assert "Amount mismatch" in deposit.failure_reason
    assert deposit.raw_rpc_response is not None
    account.refresh_from_db()
    assert account.balance == Decimal("0")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_transfer_not_found_marks_failed(account: Account) -> None:
    provider = _FakeBlockchainProvider(TransferNotFoundError("missing"))
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError, match="missing"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="not-found",
        )

    deposit = DepositRequest.objects.get(idempotency_key="not-found")
    assert deposit.status == DepositRequest.Status.FAILED


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
@pytest.mark.parametrize(
    ("error_message", "idempotency_key"),
    [
        (
            "No USDT transfer to platform wallet in " + TX_HASH,
            "wrong-token-or-dest",
        ),
        (
            f"Transaction receipt not found for {TX_HASH}",
            "missing-receipt",
        ),
    ],
)
def test_verify_provider_transfer_not_found_scenarios_mark_failed(
    account: Account, error_message: str, idempotency_key: str
) -> None:
    """Wrong token / wrong destination / missing receipt → TransferNotFound → FAILED.

    PolygonProvider raises ``TransferNotFoundError`` when there is no USDT
    Transfer log to the platform wallet (wrong token or recipient) or when the
    receipt is missing. DepositVerificationService must mark the deposit FAILED.
    """
    provider = _FakeBlockchainProvider(TransferNotFoundError(error_message))
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError, match="USDT|receipt"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key=idempotency_key,
        )

    deposit = DepositRequest.objects.get(idempotency_key=idempotency_key)
    assert deposit.status == DepositRequest.Status.FAILED
    assert deposit.failure_reason == error_message
    account.refresh_from_db()
    assert account.balance == Decimal("0")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_wrong_chain_id_marks_failed(account: Account) -> None:
    """BlockchainProviderError (e.g. unexpected chain_id) → deposit FAILED."""
    provider = _FakeBlockchainProvider(
        BlockchainProviderError("Unexpected chain_id 1; expected 137")
    )
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError, match="chain_id"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="wrong-chain",
        )

    deposit = DepositRequest.objects.get(idempotency_key="wrong-chain")
    assert deposit.status == DepositRequest.Status.FAILED
    assert "chain_id" in deposit.failure_reason
    account.refresh_from_db()
    assert account.balance == Decimal("0")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rejects_invalid_tx_hash(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)

    with pytest.raises(DepositVerificationError, match="Invalid transaction hash"):
        service.verify(
            account,
            tx_hash="0x1234",
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="bad-hash",
        )

    assert DepositRequest.objects.filter(idempotency_key="bad-hash").count() == 0
    assert provider.calls == []


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_reverted_tx_marks_failed(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer(status="reverted"))
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError, match="reverted"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="reverted",
        )

    deposit = DepositRequest.objects.get(idempotency_key="reverted")
    assert deposit.status == DepositRequest.Status.FAILED


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rpc_error_marks_failed_and_reraises(account: Account) -> None:
    provider = _FakeBlockchainProvider(BlockchainRPCError("down"))
    service = _service(provider)

    with pytest.raises(BlockchainRPCError, match="down"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="rpc-fail",
        )

    deposit = DepositRequest.objects.get(idempotency_key="rpc-fail")
    assert deposit.status == DepositRequest.Status.FAILED
    assert "RPC error" in deposit.failure_reason


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_failed_result_is_idempotent(account: Account) -> None:
    provider = _FakeBlockchainProvider(TransferNotFoundError("gone"))
    service = _service(provider)

    with pytest.raises(DepositVerificationFailedError):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="failed-once",
        )

    # Second call returns the failed deposit without hitting the provider again.
    second = service.verify(
        account,
        tx_hash=TX_HASH,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="failed-once",
    )
    assert second.status == DepositRequest.Status.FAILED
    assert len(provider.calls) == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=False)
def test_verify_billing_disabled(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)

    with pytest.raises(BillingDisabledError):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="disabled",
        )
    assert DepositRequest.objects.count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rejects_empty_idempotency_key(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)

    with pytest.raises(DepositVerificationError, match="idempotency_key"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=Decimal("10.000000"),
            idempotency_key="",
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rejects_invalid_payment_method(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer())
    service = _service(provider)

    with pytest.raises(DepositVerificationError, match="payment_method"):
        service.verify(
            account,
            tx_hash=TX_HASH,
            payment_method="cash",
            amount_requested=Decimal("10.000000"),
            idempotency_key="bad-method",
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_normalizes_tx_hash(account: Account) -> None:
    bare = "ab" * 32
    provider = _FakeBlockchainProvider(_transfer(tx_hash="0x" + bare))
    service = _service(provider)

    deposit = service.verify(
        account,
        tx_hash=bare.upper(),
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="norm-hash",
    )
    assert deposit.tx_hash == "0x" + bare
    assert provider.calls == ["0x" + bare]


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_confirmations_equal_to_min_succeeds(account: Account) -> None:
    provider = _FakeBlockchainProvider(_transfer(confirmations=20))
    service = _service(provider, min_confirmations=20)

    deposit = service.verify(
        account,
        tx_hash=TX_HASH_OTHER,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="exact-min-conf",
    )
    assert deposit.status == DepositRequest.Status.COMPLETED
