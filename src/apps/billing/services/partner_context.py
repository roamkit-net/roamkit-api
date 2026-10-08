"""Partner-channel tenant resolution (ADR 023).

Not the spend/inventory organization context. The caller passes only the
authenticated user. Channel ``is_active`` and organization status are not
filters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.billing.partner_channel import PartnerChannel
from apps.organizations.models import MembershipRole, MembershipStatus

if TYPE_CHECKING:
    from apps.accounts.models import User

_GRANT_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)
_SUMMARY_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.VIEWER)


class PartnerAccessDenied(Exception):
    """No active owner or admin membership on an organization with a channel."""

    code = "partner_access_denied"


class PartnerContextAmbiguous(Exception):
    """More than one owner/admin partner channel. The response lists none."""

    code = "partner_context_ambiguous"


def resolve_partner_grant_channel(user: User) -> PartnerChannel:
    """Return the one partner channel this user may grant from."""
    return _resolve_channel(user, _GRANT_ROLES)


def resolve_partner_summary_channel(user: User) -> PartnerChannel:
    """Return the one partner channel this user may read summary for."""
    return _resolve_channel(user, _SUMMARY_ROLES)


def partner_reader_role(user: User, partner_channel: PartnerChannel) -> str:
    """Active portal role on this channel's organization. Empty when none."""
    from apps.organizations.models import Membership

    role = (
        Membership.objects.filter(
            organization_id=partner_channel.organization_id,
            user_id=user.pk,
            status=MembershipStatus.ACTIVE,
            role__in=_SUMMARY_ROLES,
        )
        .values_list("role", flat=True)
        .first()
    )
    return role or ""


def stamp_partner_role(response, user: User, partner_channel: PartnerChannel) -> None:
    """Presentation hint only. Tenant resolution never reads this header."""
    role = partner_reader_role(user, partner_channel)
    if role in {MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.VIEWER}:
        response["X-Partner-Role"] = role


def _resolve_channel(user: User, roles: tuple[str, ...]) -> PartnerChannel:
    channels = list(
        PartnerChannel.objects.filter(
            organization__memberships__user_id=user.pk,
            organization__memberships__status=MembershipStatus.ACTIVE,
            organization__memberships__role__in=roles,
        ).distinct()
    )
    if not channels:
        raise PartnerAccessDenied("No partner channel is available for this user")
    if len(channels) > 1:
        raise PartnerContextAmbiguous("More than one partner channel matches")
    return channels[0]
