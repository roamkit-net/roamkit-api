"""eSIM / top-up domain exceptions."""


class TopupError(Exception):
    """Base error for top-up operations."""


class TopupPackageNotFoundError(TopupError):
    """Raised when the requested top-up package is not listed for the eSIM."""
