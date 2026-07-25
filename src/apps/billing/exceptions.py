"""Billing domain exceptions."""


class CreditServiceError(Exception):
    """Base error for CreditService operations."""


class InsufficientFundsError(CreditServiceError):
    """Raised when a debit would make Account.balance negative."""


class InvalidAmountError(CreditServiceError):
    """Raised when amount is not a positive Decimal(20,6) value."""


class InvalidReferenceTypeError(CreditServiceError):
    """Raised when reference_type is not a LedgerReferenceType value."""
