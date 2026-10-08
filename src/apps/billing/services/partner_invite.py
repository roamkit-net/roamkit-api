"""Partner invite link reads and owner mutations (ADR 023).

A channel may have many links. Portal reads and owner mutations use the
canonical row: smallest ``(created_at, id)``.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.partner_channel import (
    PartnerChannel,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_context import partner_reader_role
from apps.billing.services.partner_invite_visit import record_visit
from apps.organizations.models import MembershipRole, Organization

logger = logging.getLogger(__name__)

_DEFAULT_SHARE = Decimal("50.00")


class PartnerInviteForbidden(Exception):
    """Admin or viewer tried to change the invite link."""

    code = "partner_invite_forbidden"


class PartnerInviteError(Exception):
    """The invite link could not be minted."""


@dataclass(frozen=True, slots=True)
class PartnerInviteLinkView:
    url: str
    is_active: bool
    created_at: datetime
    regenerated_at: datetime | None


def require_partner_owner(user: User, partner_channel: PartnerChannel) -> None:
    if partner_reader_role(user, partner_channel) != MembershipRole.OWNER:
        raise PartnerInviteForbidden("Invite changes require an owner")


def canonical_invite_link(
    partner_channel: PartnerChannel,
    *,
    for_update: bool = False,
) -> PartnerInviteLink:
    """Return the portal link: smallest ``(created_at, id)``.

    ``.first()`` is only valid after that ``order_by``. An unordered
    ``.first()`` is not a canonical link.
    """
    qs = PartnerInviteLink.objects.filter(partner_channel=partner_channel).order_by(
        "created_at",
        "id",
    )
    if for_update:
        qs = qs.select_for_update()
    link = qs.first()
    if link is None:
        raise PartnerInviteLink.DoesNotExist("Partner channel has no invite link")
    return link


def invite_link_for(partner_channel: PartnerChannel) -> PartnerInviteLinkView:
    return _view(canonical_invite_link(partner_channel))


def regenerate_invite_link(
    partner_channel: PartnerChannel,
    *,
    actor: User,
    request_id: str | None = None,
) -> PartnerInviteLinkView:
    with transaction.atomic():
        link = canonical_invite_link(partner_channel, for_update=True)
        old_token = link.token
        link.regenerated_at = timezone.now()
        _assign_token(link)
        # Legacy bridge: pending rows written before invite_visit existed are
        # still matched by the token snapshot. Visits are not deleted.
        # The next service cut also drops a pending row whose visit predates
        # regenerated_at.
        PendingPartnerAttribution.objects.filter(
            partner_channel=partner_channel,
            invite_token_snapshot=old_token,
        ).delete()
    if request_id is not None:
        _audit("partner_invite.regenerated", actor, partner_channel, request_id)
    return _view(link)


def set_invite_active(
    partner_channel: PartnerChannel,
    *,
    actor: User,
    active: bool,
    request_id: str | None = None,
) -> PartnerInviteLinkView:
    with transaction.atomic():
        link = canonical_invite_link(partner_channel, for_update=True)
        changed = link.is_active != active
        if changed:
            link.is_active = active
            link.save(update_fields=["is_active"])
    if changed and request_id is not None:
        action = "partner_invite.activated" if active else "partner_invite.deactivated"
        _audit(action, actor, partner_channel, request_id)
    return _view(link)


def issue_join_signature(
    token: str,
    utm: Mapping[str, object] | None = None,
) -> str | None:
    """Lock the link, store one visit, and return a visit cookie. None is a 404."""
    recorded = record_visit(token, utm)
    if recorded is None:
        return None
    return recorded[1]


def create_partner_channel(
    *,
    organization: Organization,
    revenue_share_percent: Decimal = _DEFAULT_SHARE,
) -> PartnerChannel:
    """Create the channel and its one invite link, or create neither."""
    if organization.account_id is None:
        raise PartnerInviteError("Organization has no team account")
    for _ in range(5):
        try:
            with transaction.atomic():
                channel = PartnerChannel.objects.create(
                    organization=organization,
                    revenue_share_percent=revenue_share_percent,
                    is_active=True,
                )
                PartnerInviteLink.objects.create(
                    partner_channel=channel,
                    token=secrets.token_urlsafe(24),
                    name="",
                    bonus_amount=Decimal("0.000000"),
                    source="",
                    campaign="",
                    content="",
                    is_active=True,
                )
        except IntegrityError:
            continue
        else:
            return channel
    raise PartnerInviteError("Could not mint a unique invite token")


def _assign_token(link: PartnerInviteLink) -> None:
    for _ in range(5):
        link.token = secrets.token_urlsafe(24)
        try:
            with transaction.atomic():
                link.save(update_fields=["token", "regenerated_at"])
        except IntegrityError:
            continue
        else:
            return
    raise PartnerInviteError("Could not mint a unique invite token")


def _view(link: PartnerInviteLink) -> PartnerInviteLinkView:
    base = settings.PARTNER_JOIN_BASE_URL.rstrip("/")
    return PartnerInviteLinkView(
        url=f"{base}/join/{link.token}",
        is_active=link.is_active,
        created_at=link.created_at,
        regenerated_at=link.regenerated_at,
    )


def _audit(
    action: str,
    actor: User,
    partner_channel: PartnerChannel,
    request_id: str,
) -> None:
    logger.info(
        "%s actor_user_id=%s partner_channel_id=%s organization_id=%s "
        "action=%s created_at=%s request_id=%s",
        action,
        actor.pk,
        partner_channel.pk,
        partner_channel.organization_id,
        action,
        timezone.now().isoformat().replace("+00:00", "Z"),
        request_id,
    )


def _unused_integrity() -> None:
    """Keep IntegrityError imported for the token insert race retry below."""
    raise IntegrityError()
