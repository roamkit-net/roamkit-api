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
