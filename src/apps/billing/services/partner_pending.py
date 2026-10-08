"""Signed partner-pending payload (ADR 023).

The cookie value is this signature, not the raw invite token. Callers must
not log the token, the join URL, or the signed value.

The signature lives 30 days. The business attribution window is
``InviteVisit.created_at``, checked by ``validate_invite_visit``. The pending
row created at email submit still expires 24 hours after ``issued_at``.
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
SIGNATURE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
PENDING_ROW_MAX_AGE_SECONDS = 24 * 60 * 60


def sign_partner_pending(*, visit_id: UUID, issued_at: datetime) -> str:
    payload = json.dumps(
        {
            "visit_id": str(visit_id),
            "issued_at": issued_at.isoformat(),
        },
        separators=(",", ":"),
    )
    return TimestampSigner(salt=_SALT).sign(payload)


def unsign_partner_pending(value: str) -> dict[str, Any] | None:
    """Return ``visit_id`` and ``issued_at`` when the signature still holds."""
    if not value or not isinstance(value, str):
        return None
    try:
        raw = TimestampSigner(salt=_SALT).unsign(
            value, max_age=SIGNATURE_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None
    try:
        data = json.loads(raw)
        visit_id = UUID(str(data["visit_id"]))
        issued_at = parse_datetime(str(data["issued_at"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if issued_at is None:
        return None
    if set(data) - {"visit_id", "issued_at"}:
        return None
    if timezone.is_naive(issued_at):
        issued_at = timezone.make_aware(issued_at, timezone.get_current_timezone())
    return {"visit_id": visit_id, "issued_at": issued_at}


def pending_expires_at(issued_at: datetime | None = None) -> datetime:
    """24-hour pending-row deadline measured from cookie issue, not from confirm."""
    start = issued_at or timezone.now()
    return start + timedelta(seconds=PENDING_ROW_MAX_AGE_SECONDS)
