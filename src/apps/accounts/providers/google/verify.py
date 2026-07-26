"""Verify Google ID tokens (GIS credential)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from google.auth.exceptions import GoogleAuthError as GoogleLibraryAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from apps.accounts.providers.google.errors import GoogleAuthError, GoogleAuthErrorCode

logger = logging.getLogger("roamkit.auth.google")

_ALLOWED_ISSUERS = frozenset(
    {
        "accounts.google.com",
        "https://accounts.google.com",
    }
)

# Whitelist only (ADR 015) — ignore all other claims after verify.
_CLAIM_KEYS = frozenset(
    {
        "sub",
        "email",
        "email_verified",
        "name",
        "picture",
        "iss",
        "aud",
        "exp",
        "iat",
    }
)


@dataclass(frozen=True, kw_only=True)
class GoogleIdentity:
    subject: str
    email: str
    email_verified: bool
    name: str
    picture: str


class _TimeoutRequest(google_requests.Request):
    """google-auth Request with a bounded timeout."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        return super().__call__(
            url, method=method, body=body, headers=headers, **kwargs
        )


def verify_google_id_token(credential: str) -> GoogleIdentity:
    """Verify GIS ID token; return whitelisted identity. Discard raw JWT after."""
    client_id = (getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or "").strip()
    if not client_id:
        raise GoogleAuthError(GoogleAuthErrorCode.FEATURE_DISABLED)

    timeout = float(getattr(settings, "GOOGLE_OAUTH_VERIFY_TIMEOUT", 2.5))
    skew = int(getattr(settings, "GOOGLE_OAUTH_CLOCK_SKEW_SECONDS", 60))

    try:
        claims = id_token.verify_oauth2_token(
            credential,
            _TimeoutRequest(timeout),
            audience=client_id,
            clock_skew_in_seconds=skew,
        )
    except ValueError as exc:
        logger.info(
            "google_verify_failed",
            extra={
                "provider": "google",
                "result": "failed",
                "reason": "invalid_token",
            },
        )
        raise GoogleAuthError(GoogleAuthErrorCode.INVALID_TOKEN) from exc
    except (GoogleLibraryAuthError, OSError, TimeoutError) as exc:
        logger.info(
            "google_verify_failed",
            extra={
                "provider": "google",
                "result": "failed",
                "reason": "verify_unavailable",
            },
        )
        raise GoogleAuthError(GoogleAuthErrorCode.VERIFY_UNAVAILABLE) from exc

    # Keep only whitelisted claims in memory.
    filtered = {k: claims.get(k) for k in _CLAIM_KEYS if k in claims}
    del claims

    issuer = filtered.get("iss")
    if issuer not in _ALLOWED_ISSUERS:
        raise GoogleAuthError(GoogleAuthErrorCode.INVALID_TOKEN)

    subject = filtered.get("sub")
    email = filtered.get("email")
    if not isinstance(subject, str) or not subject.strip():
        raise GoogleAuthError(GoogleAuthErrorCode.INVALID_TOKEN)
    if not isinstance(email, str) or not email.strip():
        raise GoogleAuthError(GoogleAuthErrorCode.INVALID_TOKEN)

    email_verified = filtered.get("email_verified") is True
    if not email_verified:
        raise GoogleAuthError(GoogleAuthErrorCode.EMAIL_NOT_VERIFIED)

    name = filtered.get("name") if isinstance(filtered.get("name"), str) else ""
    picture = (
        filtered.get("picture") if isinstance(filtered.get("picture"), str) else ""
    )

    return GoogleIdentity(
        subject=subject.strip(),
        email=email.strip(),
        email_verified=True,
        name=name.strip(),
        picture=picture.strip(),
    )
