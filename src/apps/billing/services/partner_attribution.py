"""Pending partner attribution and consume (ADR 023).

Register writes the pending row from the invite visit. Email activation reads
that visit, not the channel's canonical link. Consume reads the signed cookie.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    InviteVisit,
    PartnerChannel,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_invite_visit import validate_invite_visit
from apps.billing.services.partner_pending import (
    pending_expires_at,
    unsign_partner_pending,
)

_CREATED = "created"
_NOOP = "noop"
_IGNORED = "ignored"


def invite_snapshot_from_visit(visit: InviteVisit) -> dict[str, Any]:
    """Copy the link and the click as they are when attribution is inserted.

    ``invite_visit`` is the click. ``invite_token`` is the link's current
    token. A visit that predates regeneration is not a valid attribution, and
    this helper does not rebuild the replaced token.

    Does not set ``registered_via_invite`` or ``bonus_amount_snapshot``.
    The returned strings are copies, so a later link edit does not change them.
    """
    link = PartnerInviteLink.objects.get(pk=visit.invite_link_id)
    return {
        "invite_visit": visit,
        "invite_token": link.token,
        "invite_name_snapshot": link.name,
        "invite_source_snapshot": link.source,
        "invite_campaign_snapshot": link.campaign,
        "invite_content_snapshot": link.content,
        "utm_source_snapshot": visit.utm_source,
        "utm_medium_snapshot": visit.utm_medium,
        "utm_campaign_snapshot": visit.utm_campaign,
        "utm_content_snapshot": visit.utm_content,
    }


def record_pending_for_new_user(user: User, signed: str | None) -> None:
    """Store one pending row for a newly created inactive user.

    The visit must still be inside the 30-day window. An invalid invite does
    not fail registration and does not write a row. An existing pending row
    or attribution is left as it is.
    """
    if not signed or not settings.PARTNER_CHANNEL_ENABLED or user.is_active:
        return
    if CustomerAttribution.objects.filter(user_id=user.pk).exists():
        return
    if PendingPartnerAttribution.objects.filter(user_id=user.pk).exists():
        return
    payload = unsign_partner_pending(signed)
    if payload is None:
        return
    with transaction.atomic():
        visit = _lock_visit(payload["visit_id"], check_attribution_window=True)
        if visit is None:
            return
        link = visit.invite_link
        PendingPartnerAttribution.objects.create(
            user=user,
            partner_channel_id=link.partner_channel_id,
            invite_token_snapshot=link.token,
            invite_visit=visit,
            expires_at=pending_expires_at(),
        )


def apply_pending_on_activation(user: User) -> None:
    """Turn a still-valid pending row into an attribution, or delete it.

    A row with ``invite_visit`` follows that click. The 30-day window is not
    checked again; ``expires_at`` is the 24-hour limit. A row without a visit
    is a legacy pending and still uses the token snapshot.
    """
    pending = PendingPartnerAttribution.objects.filter(user_id=user.pk).first()
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
    if pending.invite_visit_id is None:
        _apply_legacy_pending(user, pending)
        return
    with transaction.atomic():
        locked = (
            PendingPartnerAttribution.objects.select_for_update()
            .filter(pk=pending.pk)
            .first()
        )
        if locked is None:
            return
        if locked.expires_at <= timezone.now():
            locked.delete()
            return
        if CustomerAttribution.objects.filter(user_id=user.pk).exists():
            locked.delete()
            return
        visit = _lock_visit(
            locked.invite_visit_id,
            check_attribution_window=False,
        )
        if visit is None:
            locked.delete()
            return
        attribution = _insert_attribution(
            user,
            visit,
            registered_via_invite=True,
        )
        credit_registration_invite_bonus(attribution=attribution)
        locked.delete()


def attribute_created_invite(user: User, signed: str | None) -> None:
    """Attribute a user inserted in this invite flow. Invalid context is ignored."""
    if not signed or not settings.PARTNER_CHANNEL_ENABLED:
        return
    payload = unsign_partner_pending(signed)
    if payload is None:
        return
    with transaction.atomic():
        if CustomerAttribution.objects.filter(user_id=user.pk).exists():
            return
        visit = _lock_visit(payload["visit_id"], check_attribution_window=True)
        if visit is None:
            return
        attribution = _insert_attribution(user, visit, registered_via_invite=True)
        credit_registration_invite_bonus(attribution=attribution)


def consume_partner_pending(user: User, signed: str | None) -> str:
    """Return created, noop, or ignored.

    An existing attribution is left unchanged. A pending email registration
    wins over consume so confirmation can still mark a new registration.
    """
    if CustomerAttribution.objects.filter(user_id=user.pk).exists():
        return _NOOP
    if not signed or not settings.PARTNER_CHANNEL_ENABLED:
        return _IGNORED
    payload = unsign_partner_pending(signed)
    if payload is None:
        return _IGNORED
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            if CustomerAttribution.objects.filter(user_id=user.pk).exists():
                return _NOOP
            pending = (
                PendingPartnerAttribution.objects.select_for_update()
                .filter(user_id=user.pk)
                .first()
            )
            if pending is not None:
                return _IGNORED
            visit = _lock_visit(payload["visit_id"], check_attribution_window=True)
            if visit is None:
                return _IGNORED
            _insert_attribution(user, visit, registered_via_invite=False)
    except IntegrityError:
        return _NOOP
    return _CREATED


def transfer_customer_attribution(
    *,
    user: User,
    partner_channel: PartnerChannel,
) -> CustomerAttribution:
    """Move an existing attribution to another channel.

    Only ``partner_channel`` changes. Invite history, snapshots, and the
    registration bonus stay as they were. This is not called by consume,
    email, or Google. A missing row is not created.
    """
    with transaction.atomic():
        attribution = (
            CustomerAttribution.objects.select_for_update()
            .filter(user_id=user.pk)
            .first()
        )
        if attribution is None:
            raise CustomerAttribution.DoesNotExist(
                "Customer has no partner attribution"
            )
        if attribution.partner_channel_id == partner_channel.pk:
            return attribution
        attribution.partner_channel = partner_channel
        attribution.save(update_fields=["partner_channel"])
        return attribution


def credit_registration_invite_bonus(*, attribution: CustomerAttribution) -> None:
    """Credit a new invite registration. Existing and zero bonuses do nothing.

    The ledger row points at this attribution. The idempotency key is the user,
    so a person receives the registration bonus at most once. Callers must not
    catch ``CreditService`` errors: the surrounding transaction rolls back the
    attribution with the credit.
    """
    if attribution.registered_via_invite is not True:
        return
    amount = attribution.bonus_amount_snapshot
    if amount is None or amount <= 0:
        return
    credit_service.credit(
        attribution.user.billing_account,
        amount,
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS,
        reference_id=attribution.id,
        idempotency_key=f"invite-registration-bonus:{attribution.user_id}",
    )


def _lock_visit(visit_id, *, check_attribution_window: bool) -> InviteVisit | None:
    visit = InviteVisit.objects.filter(pk=visit_id).first()
    if visit is None:
        return None
    link = (
        PartnerInviteLink.objects.select_for_update()
        .filter(pk=visit.invite_link_id)
        .first()
    )
    if link is None:
        return None
    fresh = validate_invite_visit(
        visit.pk,
        check_attribution_window=check_attribution_window,
    )
    if fresh is None:
        return None
    fresh.invite_link = link
    return fresh


def _insert_attribution(
    user: User,
    visit: InviteVisit,
    *,
    registered_via_invite: bool,
) -> CustomerAttribution:
    link = visit.invite_link
    bonus = link.bonus_amount if registered_via_invite else None
    return CustomerAttribution.objects.create(
        user=user,
        partner_channel_id=link.partner_channel_id,
        source=CustomerAttribution.Source.INVITE_LINK,
        invite_link=link,
        registered_via_invite=registered_via_invite,
        bonus_amount_snapshot=bonus,
        attributed_at=timezone.now(),
        **invite_snapshot_from_visit(visit),
    )


def _apply_legacy_pending(user: User, pending: PendingPartnerAttribution) -> None:
    """Finish a pending row written before ``invite_visit`` existed.

    New rows always carry a visit. This path still compares the stored token
    with the channel's canonical link and does not invent invite snapshots.
    """
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


def _current_link(channel_id) -> PartnerInviteLink | None:
    """The portal canonical link, not an arbitrary row for the channel."""
    from apps.billing.services.partner_invite import canonical_invite_link

    channel = PartnerChannel.objects.filter(pk=channel_id).first()
    if channel is None:
        return None
    try:
        return canonical_invite_link(channel)
    except PartnerInviteLink.DoesNotExist:
        return None
