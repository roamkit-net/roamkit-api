"""Resolve the active human verification implementation."""

from __future__ import annotations

from django.conf import settings

from apps.accounts.services.human_verification.base import HumanVerificationService
from apps.accounts.services.human_verification.noop import NoOpHumanVerification
from apps.accounts.services.human_verification.turnstile import (
    TurnstileVerificationService,
)


def get_human_verification_service() -> HumanVerificationService:
    if getattr(settings, "TURNSTILE_ENABLED", False):
        return TurnstileVerificationService()
    return NoOpHumanVerification()
