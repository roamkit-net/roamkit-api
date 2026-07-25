"""Order / top-up spend domain exceptions."""


class SpendError(Exception):
    """Base error for prepaid spend operations."""


class SpendInProgressError(SpendError):
    """Same idempotency_key is still being fulfilled (HTTP 409)."""

    def __init__(self, message: str = "Request is still in progress") -> None:
        super().__init__(message)


class IdempotencyKeyRequiredError(SpendError):
    """Paid spend path requires a non-empty idempotency_key."""
