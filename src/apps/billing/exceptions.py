"""Billing domain exceptions."""

from __future__ import annotations

from decimal import Decimal


class CreditServiceError(Exception):
    """Base error for CreditService operations."""


class InsufficientFundsError(CreditServiceError):
    """Raised when a debit would make Account.balance negative."""

    def __init__(
        self,
        message: str = "Insufficient funds",
        *,
        account_balance: Decimal | None = None,
        amount_required: Decimal | None = None,
    ) -> None:
        self.account_balance = account_balance
        self.amount_required = amount_required
        self.amount_missing = (
            (amount_required - account_balance)
            if account_balance is not None and amount_required is not None
            else None
        )
        if account_balance is not None and amount_required is not None:
            message = (
                f"Insufficient funds: balance={account_balance} "
                f"debit={amount_required}"
            )
        super().__init__(message)

    def to_api_dict(self) -> dict[str, str]:
        """Structured 402 payload so clients need not re-fetch balance."""
        payload: dict[str, str] = {
            "code": "INSUFFICIENT_CREDITS",
            "detail": str(self),
        }
        if self.amount_required is not None:
            payload["required"] = f"{self.amount_required:.6f}"
        if self.account_balance is not None:
            payload["balance"] = f"{self.account_balance:.6f}"
        if self.amount_missing is not None:
            payload["missing"] = f"{self.amount_missing:.6f}"
        return payload


class InvalidAmountError(CreditServiceError):
    """Raised when amount is not a positive Decimal(20,6) value."""


class InvalidReferenceTypeError(CreditServiceError):
    """Raised when reference_type is not a LedgerReferenceType value."""


class DepositVerificationError(Exception):
    """Base error for DepositVerificationService operations."""


class BillingDisabledError(DepositVerificationError):
    """Raised when billing is disabled via BILLING_ENABLED."""


class DuplicateTransactionError(DepositVerificationError):
    """Raised when a tx_hash was already credited (or belongs to another deposit)."""


class InsufficientConfirmationsError(DepositVerificationError):
    """Raised when the transfer exists but confirmations are below the minimum."""

    def __init__(self, confirmations: int, required: int) -> None:
        self.confirmations = confirmations
        self.required = required
        super().__init__(f"Insufficient confirmations: {confirmations} < {required}")


class DepositVerificationFailedError(DepositVerificationError):
    """Raised when on-chain verification fails (wrong amount, reverted, missing)."""


class SubscriptionsDisabledError(CreditServiceError):
    """Raised when subscriptions are disabled via SUBSCRIPTIONS_ENABLED."""


class SubscriptionError(Exception):
    """Base error for subscription renewal operations."""


class SubscriptionNotActiveError(SubscriptionError):
    """Raised when renewing a non-active subscription."""


class VoucherError(Exception):
    """Base error for voucher redeem operations."""

    code: str = "voucher_error"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.code.replace("_", " ").capitalize()
        super().__init__(self.detail)

    def to_api_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


class VouchersDisabledError(VoucherError):
    """Raised when VOUCHERS_ENABLED is false."""

    code = "vouchers_disabled"


class VoucherInvalidError(VoucherError):
    code = "voucher_invalid"


class VoucherExpiredError(VoucherError):
    code = "voucher_expired"


class VoucherRevokedError(VoucherError):
    code = "voucher_revoked"


class VoucherReservedError(VoucherError):
    code = "voucher_reserved"


class VoucherLimitError(VoucherError):
    code = "voucher_limit"


class UnsupportedRewardType(VoucherError):
    code = "voucher_unsupported_reward"