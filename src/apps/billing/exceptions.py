"""Billing domain exceptions."""


class CreditServiceError(Exception):
    """Base error for CreditService operations."""


class InsufficientFundsError(CreditServiceError):
    """Raised when a debit would make Account.balance negative."""


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
