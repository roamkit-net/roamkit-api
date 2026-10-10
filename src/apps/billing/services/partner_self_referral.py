"""Write-time self-referral guard (ADR 024).

A person cannot hold a current CustomerAttribution to a PartnerChannel they
own or actively belong to. Margin suppression is only a defensive backstop.
This module is what attribution writes and TEAM membership activation share.

Lock order inside the caller's transaction: the User row, then PartnerChannel
rows in primary-key order. Membership rows are not locked. A membership insert
that has not committed yet is invisible, and the attribution writer blocks on
the same User row before it inserts.

An already-illegal pair is not repaired. The refused write leaves it in place.
"""

from __future__ import annotations

from collections.abc import Iterable

from apps.accounts.models import User
from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.billing.services.partner_log import log_partner_event
from apps.organizations.models import Membership, MembershipStatus, Organization


class PartnerSelfReferralConflict(Exception):
    """This write would make the user a customer of their own channel."""

    code = "partner_self_referral"


def lock_self_referral_rows(
    *,
    user: User,
    channels: Iterable[PartnerChannel],
) -> None:
    """Lock the user, then the given channels in primary-key order.

    The caller must already be inside ``transaction.atomic``. Re-locking a row
    this transaction holds does not change the order.
    """
    User.objects.select_for_update().get(pk=user.pk)
    channel_ids = sorted({channel.pk for channel in channels})
    if not channel_ids:
        return
    list(
        PartnerChannel.objects.select_for_update(of=("self",))
        .filter(pk__in=channel_ids)
        .order_by("pk")
    )


def ensure_current_attribution_allowed(*, user: User, channel: PartnerChannel) -> None:
    """Raise when ``user`` must not be a current customer of ``channel``.

    Locks the user and that channel. Does not write.
    """
    lock_self_referral_rows(user=user, channels=[channel])
    if (
        channel.kind == PartnerChannel.Kind.INDIVIDUAL
        and channel.owner_user_id == user.pk
    ):
        _log_blocked(
            "partner.self_referral.attribution_blocked",
            user=user,
            channel=channel,
        )
        raise PartnerSelfReferralConflict(
            "The channel owner cannot be attributed to that channel"
        )
    if channel.kind != PartnerChannel.Kind.TEAM or channel.organization_id is None:
        return
    active = Membership.objects.filter(
        organization_id=channel.organization_id,
        user_id=user.pk,
        status=MembershipStatus.ACTIVE,
    ).exists()
    if active:
        _log_blocked(
            "partner.self_referral.attribution_blocked",
            user=user,
            channel=channel,
        )
        raise PartnerSelfReferralConflict(
            "An active organization member cannot be attributed to its partner channel"
        )


def ensure_active_membership_allowed(*, user: User, organization: Organization) -> None:
    """Raise when activating membership would self-refer on the team channel.

    No team channel means there is no partner attribution to conflict with.
    Suspended or revoked membership is not itself a conflict; this checks
    current attribution only. Does not write.
    """
    channel = (
        PartnerChannel.objects.filter(
            kind=PartnerChannel.Kind.TEAM,
            organization_id=organization.pk,
        )
        .order_by("pk")
        .first()
    )
    if channel is None:
        return
    lock_self_referral_rows(user=user, channels=[channel])
    attributed = CustomerAttribution.objects.filter(
        user_id=user.pk,
        partner_channel_id=channel.pk,
    ).exists()
    if attributed:
        _log_blocked(
            "partner.self_referral.membership_blocked",
            user=user,
            channel=channel,
        )
        raise PartnerSelfReferralConflict(
            "A customer of this partner channel cannot be an active member"
        )


def _log_blocked(event: str, *, user: User, channel: PartnerChannel) -> None:
    log_partner_event(
        event,
        user_id=user.pk,
        partner_channel_id=channel.pk,
        kind=channel.kind,
    )
