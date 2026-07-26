"""Enforce human verification on auth endpoints."""

from __future__ import annotations

import hmac

from django.conf import settings
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.request import Request

from apps.accounts.services.human_verification import (
    HumanVerificationResult,
    get_human_verification_service,
)
from apps.accounts.services.human_verification.degraded import allow_degraded_request
from core.http.client_ip import get_client_ip
from core.http.request_id import get_or_create_request_id

INTERNAL_BYPASS_HEADER = "HTTP_X_ROAMKIT_INTERNAL"


def _bypass_enabled(request: Request) -> bool:
    secret = (getattr(settings, "TURNSTILE_BYPASS_SECRET", "") or "").strip()
    if not secret:
        return False
    provided = (request.META.get(INTERNAL_BYPASS_HEADER) or "").strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, secret)


def enforce_human_verification(request: Request, *, endpoint: str) -> None:
    """
    Run human verification for a public auth POST.

    Raises ValidationError (400) on FAIL, Throttled (429) when degraded gate blocks.
    """
    if _bypass_enabled(request):
        return

    if not getattr(settings, "TURNSTILE_ENABLED", False):
        return

    token = request.data.get("turnstile_token")
    if isinstance(token, list):
        token = token[0] if token else None

    remoteip = get_client_ip(request)
    request_id = get_or_create_request_id(request)
    result = get_human_verification_service().verify(
        token if isinstance(token, str) else None,
        remoteip=remoteip,
        request_id=request_id,
        endpoint=endpoint,
    )

    if result is HumanVerificationResult.OK:
        return

    if result is HumanVerificationResult.FAIL:
        raise ValidationError(
            {"turnstile_token": "Human verification failed. Please try again."}
        )

    if not allow_degraded_request(remoteip=remoteip, endpoint=endpoint):
        raise Throttled(detail="Too many requests. Please try again later.")
