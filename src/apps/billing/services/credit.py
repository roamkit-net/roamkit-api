"""CreditService — sole mutator of Account.balance and the credit ledger."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from apps.billing.exceptions import (
    CreditServiceError,
    InsufficientFundsError,
    InvalidAmountError,
    InvalidReferenceTypeError,
)
from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType

MONEY_QUANT = Decimal("0.000001")


class CreditService:
    """Sole mutator of ``Account.balance`` and the append-only credit ledger.

    Both ``credit`` and ``debit`` share ``_apply``: one DB transaction,
    ``select_for_update`` on the account row, ledger INSERT, then balance +
    version bump. Corrections are compensating entries, never ledger updates.
    """

    def credit(
        self,
        account: Account,
        amount: Decimal | int | str,
        *,
        reference_type: str | LedgerReferenceType,
        reference_id: str | UUID,
        idempotency_key: str,
    ) -> CreditLedgerEntry:
        """Add ``amount`` to ``account`` and append a positive ledger entry.

        ``amount`` must be > 0 and is quantized to 6 decimal places. The
        operation is idempotent on ``idempotency_key``: a repeat returns the
        original ``CreditLedgerEntry`` without changing balance again.

        Raises:
            InvalidAmountError: amount is not a positive finite Decimal(20,6).
            InvalidReferenceTypeError: ``reference_type`` is not a
                ``LedgerReferenceType`` value.
            CreditServiceError: ``idempotency_key`` is empty.
        """
        return self._apply(
            account,
            amount,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            sign=1,
        )

    def debit(
        self,
        account: Account,
        amount: Decimal | int | str,
        *,
        reference_type: str | LedgerReferenceType,
        reference_id: str | UUID,
        idempotency_key: str,
    ) -> CreditLedgerEntry:
        """Subtract ``amount`` from ``account`` and append a negative ledger entry.

        ``amount`` must be > 0 (the stored delta is negative). Idempotent on
        ``idempotency_key`` like ``credit``. Under concurrent load, row locking
        serializes debits so balance cannot go negative.

        Raises:
            InvalidAmountError: amount is not a positive finite Decimal(20,6).
            InvalidReferenceTypeError: ``reference_type`` is not a
                ``LedgerReferenceType`` value.
            CreditServiceError: ``idempotency_key`` is empty.
            InsufficientFundsError: locked balance is less than ``amount``.
        """
        return self._apply(
            account,
            amount,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            sign=-1,
        )

    def _apply(
        self,
        account: Account,
        amount: Decimal | int | str,
        *,
        reference_type: str | LedgerReferenceType,
        reference_id: str | UUID,
        idempotency_key: str,
        sign: int,
    ) -> CreditLedgerEntry:
        normalized = self._normalize_amount(amount)
        ref_type = self._normalize_reference_type(reference_type)
        ref_id = str(reference_id)
        if not idempotency_key:
            raise CreditServiceError("idempotency_key is required")

        delta = normalized if sign > 0 else -normalized

        try:
            with transaction.atomic():
                locked = Account.objects.select_for_update().get(pk=account.pk)

                existing = CreditLedgerEntry.objects.filter(
                    idempotency_key=idempotency_key
                ).first()
                if existing is not None:
                    return existing

                new_balance = locked.balance + delta
                if new_balance < 0:
                    raise InsufficientFundsError(
                        "Insufficient funds: "
                        f"balance={locked.balance} debit={normalized}"
                    )

                entry = CreditLedgerEntry(
                    account=locked,
                    delta=delta,
                    balance_after=new_balance,
                    reference_type=ref_type,
                    reference_id=ref_id,
                    idempotency_key=idempotency_key,
                )
                entry.save()

                locked.balance = new_balance
                locked.version = locked.version + 1
                locked.save(update_fields=["balance", "version", "updated_at"])
                return entry
        except IntegrityError:
            # Concurrent insert with the same idempotency_key.
            existing = CreditLedgerEntry.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing is not None:
                return existing
            raise

    @staticmethod
    def _normalize_amount(amount: Decimal | int | str) -> Decimal:
        try:
            value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise InvalidAmountError(f"Invalid amount: {amount!r}") from exc

        if not value.is_finite():
            raise InvalidAmountError("Amount must be a finite number")

        quantized = value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if quantized <= 0:
            raise InvalidAmountError("Amount must be greater than zero")
        # max_digits=20, decimal_places=6 → at most 14 digits before the point
        if quantized >= Decimal("100000000000000"):
            raise InvalidAmountError("Amount exceeds Decimal(20,6) range")
        return quantized

    @staticmethod
    def _normalize_reference_type(reference_type: str | LedgerReferenceType) -> str:
        values = {choice.value for choice in LedgerReferenceType}
        value = (
            reference_type.value
            if isinstance(reference_type, LedgerReferenceType)
            else str(reference_type)
        )
        if value not in values:
            raise InvalidReferenceTypeError(f"Unknown reference_type: {value!r}")
        return value


credit_service = CreditService()


def resolve_reference(reference_type: str, reference_id: str) -> Any | None:
    """Return the related model instance for admin deep-links, if registered."""
    from apps.billing.models import REFERENCE_MODELS

    model = REFERENCE_MODELS.get(reference_type)
    if model is None:
        return None
    try:
        return model.objects.filter(pk=reference_id).first()
    except (ValueError, TypeError):
        return None
