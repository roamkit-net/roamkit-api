"""eSIM / top-up / lifecycle domain exceptions."""


class TopupError(Exception):
    """Base error for top-up operations."""


class TopupPackageNotFoundError(TopupError):
    """Raised when the requested top-up package is not listed for the eSIM."""


class LifecycleError(Exception):
    """Base error for eSIM lifecycle operations."""


class InvalidLifecycleTransitionError(LifecycleError):
    """Raised when a status transition is not allowed."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid lifecycle transition: {current} → {target}")


class UnknownLifecycleEventTypeError(LifecycleError):
    """Raised when a client event_type is not allowlisted."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        super().__init__(event_type)
