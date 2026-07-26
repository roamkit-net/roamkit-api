"""Human verification interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum


class HumanVerificationResult(StrEnum):
    OK = "ok"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class HumanVerificationService(ABC):
    """Provider-agnostic human verification (Turnstile, hCaptcha, …)."""

    @abstractmethod
    def verify(
        self,
        token: str | None,
        *,
        remoteip: str,
        request_id: str,
        endpoint: str,
    ) -> HumanVerificationResult:
        """Verify a client token. Never raise for CF outages — return UNAVAILABLE."""
