"""Invite clicks, UTM capture, and the one visit validator.

Email, Google, and consume must call ``validate_invite_visit`` instead of
copying these checks. This module does not credit a bonus.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.partner_channel import (
    InviteVisit,
    PartnerInviteLink,
    partner_invite_token_is_usable,
)
from apps.billing.services.partner_pending import sign_partner_pending

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")
UTM_MAX_LENGTH = 128
ATTRIBUTION_WINDOW = timedelta(days=30)


def normalize_utm_value(value: object) -> str:
    """Keep the sent text. Missing or empty becomes ``""``. Longer than 128 is cut.

    A list or tuple is a repeated query parameter; only the first value is kept.
    No trim, lowercase, or campaign-name cleanup.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if value is None:
        return ""
    text = str(value)
    if text == "":
        return ""
    return text[:UTM_MAX_LENGTH]


def normalize_utm(params: Mapping[str, object] | None) -> dict[str, str]:
    source = params or {}
    return {field: normalize_utm_value(source.get(field)) for field in UTM_FIELDS}


def record_visit(
    token: str,
    utm: Mapping[str, object] | None = None,
) -> tuple[InviteVisit, str] | None:
    """Lock the link row, insert one visit, and sign ``visit_id`` plus ``issued_at``.

    Unknown or inactive tokens insert nothing. The same lock serializes regenerate.
    """
    if (
        not partner_invite_token_is_usable(token)
        or not settings.PARTNER_CHANNEL_ENABLED
    ):
        return None
    utm_values = normalize_utm(utm)
    with transaction.atomic():
        link = PartnerInviteLink.objects.select_for_update().filter(token=token).first()
        if link is None or not link.is_active:
            return None
        visit = InviteVisit.objects.create(invite_link=link, **utm_values)
        issued_at = timezone.now()
        signed = sign_partner_pending(visit_id=visit.id, issued_at=issued_at)
    return visit, signed


def visit_predates_regeneration(visit: InviteVisit) -> bool:
    regenerated_at = visit.invite_link.regenerated_at
    return regenerated_at is not None and visit.created_at < regenerated_at


def validate_invite_visit(
    visit_id: UUID | str,
    *,
    check_attribution_window: bool = True,
) -> InviteVisit | None:
    """Return the visit when it may still be used, otherwise ``None``.

    ``check_attribution_window=True`` is email submit, Google, and consume:
    the click must be within 30 days. ``False`` is email confirmation: the
    pending row already carries that right, so age is not checked again.
    Neither mode deletes the visit.
    """
    try:
        parsed = visit_id if isinstance(visit_id, UUID) else UUID(str(visit_id))
    except (TypeError, ValueError):
        return None
    visit = InviteVisit.objects.select_related("invite_link").filter(pk=parsed).first()
    if visit is None or not visit.invite_link.is_active:
        return None
    if visit_predates_regeneration(visit):
        return None
    too_old = visit.created_at < timezone.now() - ATTRIBUTION_WINDOW
    if check_attribution_window and too_old:
        return None
    return visit
