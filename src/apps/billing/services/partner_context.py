"""Partner-channel tenant resolution.

ADR 023 helpers still require exactly one team membership channel.
ADR 024 list and resolve helpers return every channel the user may access.
They do not replace the single-channel helpers.

Not the spend/inventory organization context. The caller passes only the
authenticated user. Channel ``is_active`` and organization status are not
access filters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Exists, OuterRef, Subquery

from apps.billing.partner_channel import PartnerChannel
from apps.billing.services.partner_log import log_partner_event
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
)

if TYPE_CHECKING:
    from apps.accounts.models import User

_GRANT_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)
_PLANS_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)
_SUMMARY_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.VIEWER)
_CUSTOMERS_ROLES = (
    MembershipRole.OWNER,
    MembershipRole.ADMIN,
    MembershipRole.VIEWER,
    MembershipRole.MEMBER,
)
_ROLE_HEADER = frozenset(
    {
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
        MembershipRole.VIEWER,
        MembershipRole.MEMBER,
    }
)


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


def resolve_partner_customers_channel(user: User) -> PartnerChannel:
    """Return the one team channel this user may read customers for.

    Includes an active member. Does not authorize summary, grants, or invite.
    """
    return _resolve_channel(user, _CUSTOMERS_ROLES)


def partner_customers_role(user: User, channel: PartnerChannel) -> str:
    """Role that authorized a customers read of ``channel``."""
    if _individual_owner_matches(channel, user):
        return MembershipRole.OWNER
    role = _active_membership_role(user, channel, _CUSTOMERS_ROLES)
    if role is None:
        raise PartnerAccessDenied(_ACCESS_DENIED)
    return role


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
    apply_partner_role_header(response, partner_reader_role(user, partner_channel))


def apply_partner_role_header(response, role: str) -> None:
    """Set ``X-Partner-Role`` when ``role`` is a portal or customers role."""
    if role in _ROLE_HEADER:
        # wsgiref requires a plain str. TextChoices members are str subclasses.
        response["X-Partner-Role"] = str(role)


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


_ACCESS_DENIED = "No partner channel is available for this user"


@dataclass(frozen=True, slots=True)
class PartnerContext:
    """One PartnerChannel this user may access.

    Access is not operational eligibility. ``effective_role`` is the ADR 023
    portal role (an individual owner is ``owner``). It does not by itself
    allow a grant or an invite-link write. Settlement is also absent: money
    code calls ``resolve_partner_settlement_account`` on ``channel``.
    """

    channel: PartnerChannel
    channel_id: uuid.UUID
    kind: str
    label: str
    effective_role: str


def list_authorized_partner_contexts(user: User) -> tuple[PartnerContext, ...]:
    """Return every channel this user may access. Empty is a normal result.

    An individual context is included only when ``owner_user`` is this user.
    A team context is included only for an active owner, admin, or viewer
    membership. Several contexts are returned together. This does not raise
    ``PartnerContextAmbiguous`` and does not pick a default channel.
    """
    contexts: list[PartnerContext] = []
    individual = (
        PartnerChannel.objects.filter(
            kind=PartnerChannel.Kind.INDIVIDUAL,
            owner_user_id=user.pk,
        )
        .select_related("owner_user")
        .order_by("id")
        .first()
    )
    if individual is not None and _individual_owner_matches(individual, user):
        contexts.append(
            _partner_context(individual, role=MembershipRole.OWNER),
        )
    contexts.extend(_team_contexts(user))
    return tuple(contexts)


def resolve_authorized_plans_context(
    user: User,
    channel_id: uuid.UUID,
) -> PartnerContext:
    """Return the channel for a customer plans read.

    Owner and admin only. This does not authorize the customers list, summary,
    grants, or invite, and it does not use grant permission.
    """
    return _resolve_authorized_context(user, channel_id, _PLANS_ROLES)


def resolve_authorized_customers_context(
    user: User,
    channel_id: uuid.UUID,
) -> PartnerContext:
    """Return the channel for a customers read, including an active member."""
    return _resolve_authorized_context(user, channel_id, _CUSTOMERS_ROLES)


def resolve_authorized_partner_context(
    user: User,
    channel_id: uuid.UUID,
) -> PartnerContext:
    """Return the requested channel when this user may access the portal.

    ``channel_id`` only selects the row. Authorization is loaded again on
    every call. A missing id, another user's channel, and a membership that
    is no longer active all raise ``PartnerAccessDenied``. No other channel
    is substituted. An active member is not a portal role; customers uses
    ``resolve_authorized_customers_context``.
    """
    return _resolve_authorized_context(user, channel_id, _SUMMARY_ROLES)


def _resolve_authorized_context(
    user: User,
    channel_id: uuid.UUID,
    roles: tuple[str, ...],
) -> PartnerContext:
    channel = (
        PartnerChannel.objects.filter(pk=channel_id)
        .select_related("owner_user", "organization")
        .first()
    )
    if channel is None:
        _log_access_denied(user, channel_id)
        raise PartnerAccessDenied(_ACCESS_DENIED)
    if channel.kind == PartnerChannel.Kind.INDIVIDUAL:
        if not _individual_owner_matches(channel, user):
            _log_access_denied(user, channel_id)
            raise PartnerAccessDenied(_ACCESS_DENIED)
        return _partner_context(channel, role=MembershipRole.OWNER)
    if channel.kind == PartnerChannel.Kind.TEAM:
        role = _active_membership_role(user, channel, roles)
        if role is None:
            _log_access_denied(user, channel_id)
            raise PartnerAccessDenied(_ACCESS_DENIED)
        return _partner_context(channel, role=role)
    _log_access_denied(user, channel_id)
    raise PartnerAccessDenied(_ACCESS_DENIED)


def _log_access_denied(user: User, channel_id: uuid.UUID) -> None:
    """Same event for a missing channel and an inaccessible one."""
    log_partner_event(
        "partner.context.access_denied",
        user_id=user.pk,
        requested_channel_id=channel_id,
    )


def partner_role_can_view_customer_plans(role: str) -> bool:
    """Role capability only. This is not grant authorization.

    ADR 024 allows owner and admin to read customer plans. This predicate
    does not read the channel, the feature flag, attribution, or membership
    status. Callers still apply those checks.
    """
    return role in {MembershipRole.OWNER, MembershipRole.ADMIN}


def partner_role_can_grant(role: str) -> bool:
    """Role capability only. This is not complete grant authorization.

    ADR 023 allows owner and admin to grant, including when
    ``PartnerChannel.is_active`` is false. That flag stops new accruals only.
    This predicate does not read the channel, the feature flag, attribution,
    membership status, or balance. Callers still apply those checks.
    """
    return role in {MembershipRole.OWNER, MembershipRole.ADMIN}


def partner_role_can_manage_invite(role: str) -> bool:
    """Role capability only. This is not complete invite authorization.

    ADR 023 allows only the owner to regenerate, activate, or deactivate the
    canonical invite link. ``PartnerChannel.is_active`` does not gate those
    writes. This predicate does not read the channel, the feature flag, or
    the link. Callers still apply those checks.
    """
    return role == MembershipRole.OWNER


def _individual_owner_matches(channel: PartnerChannel, user: User) -> bool:
    return (
        channel.kind == PartnerChannel.Kind.INDIVIDUAL
        and channel.owner_user_id == user.pk
        and channel.organization_id is None
    )


def _active_membership_role(
    user: User,
    channel: PartnerChannel,
    roles: tuple[str, ...],
) -> str | None:
    if (
        channel.kind != PartnerChannel.Kind.TEAM
        or channel.organization_id is None
        or channel.owner_user_id is not None
    ):
        return None
    role = (
        Membership.objects.filter(
            organization_id=channel.organization_id,
            user_id=user.pk,
            status=MembershipStatus.ACTIVE,
            role__in=roles,
        )
        .values_list("role", flat=True)
        .first()
    )
    return role or None


def _team_contexts(user: User) -> list[PartnerContext]:
    """Channels this user may open. Member is listed for the customers read."""
    membership = Membership.objects.filter(
        organization_id=OuterRef("organization_id"),
        user_id=user.pk,
        status=MembershipStatus.ACTIVE,
        role__in=_CUSTOMERS_ROLES,
    )
    channels = (
        PartnerChannel.objects.filter(kind=PartnerChannel.Kind.TEAM)
        .filter(Exists(membership))
        .select_related("organization")
        .annotate(
            authorized_membership_role=Subquery(membership.values("role")[:1]),
        )
        .order_by("organization__name", "id")
    )
    contexts: list[PartnerContext] = []
    for channel in channels:
        role = getattr(channel, "authorized_membership_role", None)
        if role not in _CUSTOMERS_ROLES or channel.owner_user_id is not None:
            continue
        contexts.append(_partner_context(channel, role=role))
    return contexts


def _partner_context(channel: PartnerChannel, *, role: str) -> PartnerContext:
    if channel.kind == PartnerChannel.Kind.INDIVIDUAL:
        label = channel.owner_user.display_label()
    else:
        label = channel.organization.name
    return PartnerContext(
        channel=channel,
        channel_id=channel.pk,
        kind=channel.kind,
        label=label,
        effective_role=str(role),
    )
