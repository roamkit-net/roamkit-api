"""Signed partner-pending payload (ADR 023).

The cookie value is this signature, not the raw invite token. Callers must
not log the token, the join URL, or the signed value.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils import timezone
from django.utils.dateparse import parse_datetime

_SALT = "roamkit.partner-pending"
_MAX_AGE_SECONDS = 24 * 60 * 60


def sign_partner_pending(
    *,
    channel_id: UUID,
    token: str,
    expires_at: datetime,
) -> str:
    payload = json.dumps(
        {
            "channel_id": str(channel_id),
            "token": token,
            "expires_at": expires_at.isoformat(),
        },
        separators=(",", ":"),
    )
    return TimestampSigner(salt=_SALT).sign(payload)


def unsign_partner_pending(value: str) -> dict[str, Any] | None:
    """Return the payload when the signature and expiry still hold."""
    if not value or not isinstance(value, str):
        return None
    try:
        raw = TimestampSigner(salt=_SALT).unsign(value, max_age=_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    try:
        data = json.loads(raw)
        channel_id = UUID(str(data["channel_id"]))
        token = data["token"]
        expires_at = parse_datetime(str(data["expires_at"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(token, str) or not token or expires_at is None:
        return None
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
    if expires_at <= timezone.now():
        return None
    return {
        "channel_id": channel_id,
        "token": token,
        "expires_at": expires_at,
    }


def pending_expires_at() -> datetime:
    return timezone.now() + timedelta(seconds=_MAX_AGE_SECONDS)
