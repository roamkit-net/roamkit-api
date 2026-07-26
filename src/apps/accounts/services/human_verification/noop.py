"""No-op verifier when human verification is disabled."""

from __future__ import annotations

from apps.accounts.services.human_verification.base import (
    HumanVerificationResult,
    HumanVerificationService,
)


class NoOpHumanVerification(HumanVerificationService):
    def verify(
        self,
        token: str | None,
        *,
        remoteip: str,
        request_id: str,
        endpoint: str,
    ) -> HumanVerificationResult:
        return HumanVerificationResult.OK
