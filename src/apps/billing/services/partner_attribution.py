"""Pending partner attribution and consume (ADR 023).

Register writes the pending row. Email activation reads that row.
An authenticated consume reads the signed cookie payload, not a client id.
"""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_pending import unsign_partner_pending

_CREATED = "created"
_NOOP = "noop"
_IGNORED = "ignored"


def record_pending_for_new_user(user: User, signed: str | None) -> None:
    """Store one pending row for a newly created inactive user.

    An existing pending row or attribution is left as it is. A later invite
    does not overwrite that context.
    """
    if not signed or not settings.PARTNER_CHANNEL_ENABLED or user.is_active:
        return
    if CustomerAttribution.objects.filter(user_id=user.pk).exists():
        return
    if PendingPartnerAttribution.objects.filter(user_id=user.pk).exists():
        return
    data = unsign_partner_pending(signed)
    if data is None:
        return
    link = _current_link(data["channel_id"])
    if link is None or not link.is_active or link.token != data["token"]:
        return
    PendingPartnerAttribution.objects.create(
        user=user,
        partner_channel_id=data["channel_id"],
        invite_token_snapshot=data["token"],
        expires_at=data["expires_at"],
    )


def apply_pending_on_activation(user: User) -> None:
    """Turn a still-valid pending row into an attribution, or delete it."""
    pending = (
        PendingPartnerAttribution.objects.select_related("partner_channel")
        .filter(user_id=user.pk)
        .first()
    )
    if pending is None:
        return
    if pending.expires_at <= timezone.now():
        pending.delete()
        return
    if CustomerAttribution.objects.filter(user_id=user.pk).exists():
        pending.delete()
        return
    if not settings.PARTNER_CHANNEL_ENABLED:
        return
    link = _current_link(pending.partner_channel_id)
    if (
        link is None
        or not link.is_active
        or link.token != pending.invite_token_snapshot
    ):
        pending.delete()
        return
    with transaction.atomic():
        locked = (
            PendingPartnerAttribution.objects.select_for_update()
            .filter(pk=pending.pk)
            .first()
        )
        if locked is None:
            return
        if CustomerAttribution.objects.filter(user_id=user.pk).exists():
            locked.delete()
            return
        CustomerAttribution.objects.create(
            user=user,
            partner_channel_id=locked.partner_channel_id,
            source=CustomerAttribution.Source.INVITE_LINK,
            invite_link=link,
            invite_token=locked.invite_token_snapshot,
            attributed_at=timezone.now(),
        )
        locked.delete()


def consume_partner_pending(user: User, signed: str | None) -> str:
    """Return created, noop, or ignored. Does not change an existing attribution."""
    if CustomerAttribution.objects.filter(user_id=user.pk).exists():
        PendingPartnerAttribution.objects.filter(user_id=user.pk).delete()
        return _NOOP
    data = unsign_partner_pending(signed or "")
    if data is None or not settings.PARTNER_CHANNEL_ENABLED:
        return _IGNORED
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            if CustomerAttribution.objects.filter(user_id=user.pk).exists():
                PendingPartnerAttribution.objects.filter(user_id=user.pk).delete()
                return _NOOP
            link = (
                PartnerInviteLink.objects.select_for_update()
                .filter(partner_channel_id=data["channel_id"])
                .first()
            )
            if (
                link is None
                or not link.is_active
                or link.token != data["token"]
                or data["expires_at"] <= timezone.now()
            ):
                return _IGNORED
            CustomerAttribution.objects.create(
                user=user,
                partner_channel_id=data["channel_id"],
                source=CustomerAttribution.Source.INVITE_LINK,
                invite_link=link,
                invite_token=data["token"],
                attributed_at=timezone.now(),
            )
            PendingPartnerAttribution.objects.filter(user_id=user.pk).delete()
    except IntegrityError:
        return _NOOP
    return _CREATED


def _current_link(channel_id) -> PartnerInviteLink | None:
    return PartnerInviteLink.objects.filter(partner_channel_id=channel_id).first()
