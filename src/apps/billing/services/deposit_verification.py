"""DepositVerificationService — Polygon USDT deposit verify + credit (ADR-010)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.exceptions import (
    AmountMismatchError,
    BillingDisabledError,
    DepositVerificationError,
    DepositVerificationFailedError,
    DuplicateTransactionError,
    InsufficientConfirmationsError,
)
from apps.billing.models import Account, DepositRequest, LedgerReferenceType
from apps.billing.services.credit import CreditService, credit_service
from shared.events.billing_events import CreditGranted, DepositVerified
from shared.events.event_bus import event_bus
from shared.providers.blockchain import (
    BlockchainProvider,
    BlockchainProviderError,
    BlockchainRPCError,
    TransferNotFoundError,
    TransferResult,
)
from shared.providers.factory import get_blockchain_provider

logger = logging.getLogger(__name__)


class DepositVerificationService:
    """Verify an on-chain USDT transfer and credit the billing account.

    Flow: idempotency gate → fetch transfer via ``BlockchainProvider`` →
    persist ``raw_rpc_response`` → exact amount + min confirmations →
    ``CreditService.credit`` in one DB transaction → snapshot events.
    """

    def __init__(
        self,
        *,
        blockchain_provider: BlockchainProvider | None = None,
        credits: CreditService | None = None,
        min_confirmations: int | None = None,
    ) -> None:
        self._provider = blockchain_provider
        self._credits = credits or credit_service
        self._min_confirmations = min_confirmations

    @property
    def provider(self) -> BlockchainProvider:
        if self._provider is None:
            self._provider = get_blockchain_provider()
        return self._provider

    @property
    def min_confirmations(self) -> int:
        if self._min_confirmations is None:
            return int(settings.POLYGON_MIN_CONFIRMATIONS)
        return self._min_confirmations

    def reverify(self, deposit: DepositRequest) -> DepositRequest:
        """Ops re-verify for PENDING (retry) or FAILED (reset + retry).

        ``COMPLETED`` deposits are returned unchanged. Idempotent credits use
        the existing ``deposit:{id}`` ledger key.
        """
        if not settings.BILLING_ENABLED:
            raise BillingDisabledError("Billing is disabled")

        with transaction.atomic():
            locked = DepositRequest.objects.select_for_update().get(pk=deposit.pk)
            if locked.status == DepositRequest.Status.COMPLETED:
                return locked
            if locked.status == DepositRequest.Status.FAILED:
                if not locked.tx_hash:
                    raise DepositVerificationError(
                        "Cannot re-verify a failed deposit without tx_hash"
                    )
                locked.status = DepositRequest.Status.PENDING
                locked.failure_reason = ""
                locked.save(update_fields=["status", "failure_reason", "updated_at"])
            deposit = locked

        return self._verify_pending(deposit)

    def verify(
        self,
        account: Account,
        *,
        tx_hash: str,
        payment_method: str,
        amount_requested: Decimal | int | str,
        idempotency_key: str,
    ) -> DepositRequest:
        """Verify ``tx_hash`` and credit ``account`` when on-chain checks pass.

        Idempotent on ``idempotency_key`` for ``COMPLETED`` results (no second
        ledger credit). ``FAILED`` deposits for the **same account** may be
        recovered by correcting ``amount_requested`` and re-running verification
        (exact-match rule unchanged). Completed ``tx_hash`` values cannot be
        credited twice.

        Raises:
            BillingDisabledError: ``BILLING_ENABLED`` is false.
            DepositVerificationError: missing idempotency_key / invalid method.
            InvalidAmountError: amount_requested is not a valid positive money.
            DuplicateTransactionError: ``tx_hash`` already credited elsewhere.
            InsufficientConfirmationsError: transfer found but under-confirmed
                (deposit stays ``PENDING`` for retry).
            AmountMismatchError: on-chain amount != requested (deposit ``FAILED``).
            DepositVerificationFailedError: permanent verification failure
                (deposit marked ``FAILED``).
            BlockchainRPCError: RPC failed after retries (deposit marked
                ``FAILED``).
        """
        if not settings.BILLING_ENABLED:
            raise BillingDisabledError("Billing is disabled")
        if not idempotency_key:
            raise DepositVerificationError("idempotency_key is required")

        method = self._normalize_payment_method(payment_method)
        amount = self._credits._normalize_amount(amount_requested)
        normalized_hash = _normalize_tx_hash(tx_hash)

        existing = DepositRequest.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            if existing.status == DepositRequest.Status.COMPLETED:
                return existing
            if existing.status == DepositRequest.Status.FAILED:
                if existing.account_id != account.id:
                    return existing
                # Same amount → permanent failure stays failed (no RPC burn).
                # Corrected amount → self-service recovery (exact-match still applies).
                if existing.amount_requested == amount:
                    return existing
                deposit = self._recover_failed_deposit(
                    existing,
                    account=account,
                    amount_requested=amount,
                    idempotency_key=idempotency_key,
                )
            else:
                deposit = existing
        else:
            failed_for_tx = (
                DepositRequest.objects.filter(
                    tx_hash=normalized_hash,
                    status=DepositRequest.Status.FAILED,
                )
                .select_related("account")
                .first()
            )
            if failed_for_tx is not None:
                if failed_for_tx.account_id != account.id:
                    raise DuplicateTransactionError(
                        f"Transaction already submitted: {normalized_hash}"
                    )
                if failed_for_tx.amount_requested == amount:
                    return failed_for_tx
                deposit = self._recover_failed_deposit(
                    failed_for_tx,
                    account=account,
                    amount_requested=amount,
                    idempotency_key=idempotency_key,
                )
            else:
                deposit = self._create_pending_deposit(
                    account=account,
                    tx_hash=normalized_hash,
                    payment_method=method,
                    amount_requested=amount,
                    idempotency_key=idempotency_key,
                )
                if deposit.status != DepositRequest.Status.PENDING:
                    return deposit

        return self._verify_pending(deposit)

    def _recover_failed_deposit(
        self,
        deposit: DepositRequest,
        *,
        account: Account,
        amount_requested: Decimal,
        idempotency_key: str,
    ) -> DepositRequest:
        """Reset a same-account FAILED deposit for amount-correction retry."""
        if deposit.account_id != account.id:
            raise DuplicateTransactionError(
                f"Transaction already submitted: {deposit.tx_hash}"
            )
        if not deposit.tx_hash:
            raise DepositVerificationError(
                "Cannot recover a failed deposit without tx_hash"
            )

        with transaction.atomic():
            locked = DepositRequest.objects.select_for_update().get(pk=deposit.pk)
            if locked.status == DepositRequest.Status.COMPLETED:
                return locked
            if locked.status != DepositRequest.Status.FAILED:
                return locked
            if locked.account_id != account.id:
                raise DuplicateTransactionError(
                    f"Transaction already submitted: {locked.tx_hash}"
                )

            locked.amount_requested = amount_requested
            locked.status = DepositRequest.Status.PENDING
            locked.failure_reason = ""
            update_fields = [
                "amount_requested",
                "status",
                "failure_reason",
                "updated_at",
            ]
            if locked.idempotency_key != idempotency_key:
                # Client issued a fresh key after FAILED — bind it to this row.
                locked.idempotency_key = idempotency_key
                update_fields.append("idempotency_key")
            locked.save(update_fields=update_fields)
            return locked

    def _create_pending_deposit(
        self,
        *,
        account: Account,
        tx_hash: str,
        payment_method: str,
        amount_requested: Decimal,
        idempotency_key: str,
    ) -> DepositRequest:
        completed = DepositRequest.objects.filter(
            tx_hash=tx_hash,
            status=DepositRequest.Status.COMPLETED,
        ).first()
        if completed is not None:
            raise DuplicateTransactionError(f"Transaction already credited: {tx_hash}")

        try:
            with transaction.atomic():
                return DepositRequest.objects.create(
                    account=account,
                    amount_requested=amount_requested,
                    payment_method=payment_method,
                    tx_hash=tx_hash,
                    idempotency_key=idempotency_key,
                    status=DepositRequest.Status.PENDING,
                )
        except IntegrityError:
            raced = DepositRequest.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if raced is not None:
                return raced
            other = DepositRequest.objects.filter(tx_hash=tx_hash).first()
            if other is not None:
                if other.status == DepositRequest.Status.COMPLETED:
                    raise DuplicateTransactionError(
                        f"Transaction already credited: {tx_hash}"
                    ) from None
                raise DuplicateTransactionError(
                    f"Transaction already submitted: {tx_hash}"
                ) from None
            raise

    def _verify_pending(self, deposit: DepositRequest) -> DepositRequest:
        tx_hash = deposit.tx_hash
        if not tx_hash:
            reason = "Deposit is missing tx_hash"
            self._mark_failed(deposit, reason)
            raise DepositVerificationFailedError(reason)

        try:
            transfer = self.provider.fetch_usdt_transfer(tx_hash)
        except TransferNotFoundError as exc:
            self._mark_failed(deposit, str(exc))
            raise DepositVerificationFailedError(str(exc)) from exc
        except BlockchainRPCError as exc:
            self._mark_failed(deposit, f"RPC error: {exc}")
            raise
        except BlockchainProviderError as exc:
            self._mark_failed(deposit, str(exc))
            raise DepositVerificationFailedError(str(exc)) from exc

        deposit.raw_rpc_response = transfer.raw_rpc_response
        deposit.save(update_fields=["raw_rpc_response", "updated_at"])

        if transfer.status != "success":
            reason = f"Transaction status is {transfer.status!r}"
            self._mark_failed(deposit, reason, raw=transfer.raw_rpc_response)
            raise DepositVerificationFailedError(reason)

        if transfer.confirmations < self.min_confirmations:
            raise InsufficientConfirmationsError(
                transfer.confirmations, self.min_confirmations
            )

        if transfer.amount != deposit.amount_requested:
            reason = (
                "Amount mismatch: on-chain "
                f"{transfer.amount} != requested {deposit.amount_requested}"
            )
            self._mark_failed(deposit, reason, raw=transfer.raw_rpc_response)
            raise AmountMismatchError(transfer.amount, deposit.amount_requested)

        return self._complete(deposit, transfer)

    def _complete(
        self, deposit: DepositRequest, transfer: TransferResult
    ) -> DepositRequest:
        events: list[DepositVerified | CreditGranted] = []

        with transaction.atomic():
            locked = (
                DepositRequest.objects.select_for_update()
                .select_related("account")
                .get(pk=deposit.pk)
            )
            if locked.status == DepositRequest.Status.COMPLETED:
                return locked
            if locked.status == DepositRequest.Status.FAILED:
                return locked

            # Guard against another request completing the same tx_hash first.
            other_completed = (
                DepositRequest.objects.select_for_update()
                .filter(
                    tx_hash=locked.tx_hash,
                    status=DepositRequest.Status.COMPLETED,
                )
                .exclude(pk=locked.pk)
                .exists()
            )
            if other_completed:
                locked.status = DepositRequest.Status.FAILED
                locked.failure_reason = (
                    f"Transaction already credited: {locked.tx_hash}"
                )
                locked.raw_rpc_response = transfer.raw_rpc_response
                locked.save(
                    update_fields=[
                        "status",
                        "failure_reason",
                        "raw_rpc_response",
                        "updated_at",
                    ]
                )
                raise DuplicateTransactionError(locked.failure_reason)

            entry = self._credits.credit(
                locked.account,
                transfer.amount,
                reference_type=LedgerReferenceType.DEPOSIT,
                reference_id=str(locked.pk),
                idempotency_key=f"deposit:{locked.pk}",
            )

            locked.amount_credited = transfer.amount
            locked.status = DepositRequest.Status.COMPLETED
            locked.verified_at = timezone.now()
            locked.failure_reason = ""
            locked.raw_rpc_response = transfer.raw_rpc_response
            locked.save(
                update_fields=[
                    "amount_credited",
                    "status",
                    "verified_at",
                    "failure_reason",
                    "raw_rpc_response",
                    "updated_at",
                ]
            )

            events.append(
                DepositVerified(
                    deposit_id=str(locked.pk),
                    account_id=str(locked.account_id),
                    amount=transfer.amount,
                    balance_after=entry.balance_after,
                    tx_hash=locked.tx_hash or transfer.tx_hash,
                    payment_method=locked.payment_method,
                    ledger_entry_id=str(entry.pk),
                    verified_at=locked.verified_at,
                )
            )
            events.append(
                CreditGranted(
                    account_id=str(locked.account_id),
                    amount=transfer.amount,
                    balance_after=entry.balance_after,
                    reference_type=LedgerReferenceType.DEPOSIT,
                    reference_id=str(locked.pk),
                    ledger_entry_id=str(entry.pk),
                    created_at=entry.created_at,
                )
            )
            deposit = locked

        for event in events:
            event_bus.publish(event)

        # ADR 018 Phase 1: shadow dual-path after legacy Credits are final.
        # Failures must never affect ADR 010 success / latency path.
        try:
            from apps.wallet.services.shadow import safe_compare_legacy_deposit

            safe_compare_legacy_deposit(deposit=deposit, transfer=transfer)
        except Exception:  # noqa: BLE001 — never break production money path
            logger.exception(
                "wallet shadow hook failed deposit_id=%s",
                deposit.pk,
            )
        return deposit

    def _mark_failed(
        self,
        deposit: DepositRequest,
        reason: str,
        *,
        raw: dict[str, Any] | None = None,
    ) -> DepositRequest:
        with transaction.atomic():
            locked = DepositRequest.objects.select_for_update().get(pk=deposit.pk)
            if locked.status == DepositRequest.Status.COMPLETED:
                return locked
            locked.status = DepositRequest.Status.FAILED
            locked.failure_reason = reason
            update_fields = ["status", "failure_reason", "updated_at"]
            if raw is not None:
                locked.raw_rpc_response = raw
                update_fields.append("raw_rpc_response")
            elif (
                deposit.raw_rpc_response is not None and locked.raw_rpc_response is None
            ):
                locked.raw_rpc_response = deposit.raw_rpc_response
                update_fields.append("raw_rpc_response")
            locked.save(update_fields=update_fields)
            return locked

    @staticmethod
    def _normalize_payment_method(payment_method: str) -> str:
        values = {choice.value for choice in DepositRequest.PaymentMethod}
        value = str(payment_method)
        if value not in values:
            raise DepositVerificationError(f"Unknown payment_method: {value!r}")
        return value


deposit_verification_service = DepositVerificationService()


def _normalize_tx_hash(value: str) -> str:
    text = (value or "").strip().lower()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66:
        raise DepositVerificationError(f"Invalid transaction hash: {value!r}")
    try:
        int(text, 16)
    except ValueError as exc:
        raise DepositVerificationError(f"Invalid transaction hash: {value!r}") from exc
    return text
