"""Human verification providers (Turnstile today; swappable later)."""

from apps.accounts.services.human_verification.base import (
    HumanVerificationResult,
    HumanVerificationService,
)
from apps.accounts.services.human_verification.factory import (
    get_human_verification_service,
)

__all__ = [
    "HumanVerificationResult",
    "HumanVerificationService",
    "get_human_verification_service",
]
