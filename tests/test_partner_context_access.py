"""Authorized partner contexts (ADR 024). Access is not mutation permission."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.billing.models import Account, CreditLedgerEntry
from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    list_authorized_partner_contexts,
    partner_role_can_grant,
    partner_role_can_manage_invite,
    resolve_authorized_customers_context,
    resolve_authorized_partner_context,
    resolve_partner_summary_channel,
)
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services.account_binding import create_organization

pytestmark = pytest.mark.django_db


def _user(prefix: str, *, display_name: str = "") -> User:
    user = User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="secret123",
    )
    if display_name:
        user.display_name = display_name
        user.save(update_fields=["display_name"])
    return user


def _individual(owner: User) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("25.00"),
    )


def _team(actor: User, name: str) -> PartnerChannel:
    org = create_organization(name=name, actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
    )


def _membership(
    *,
    user: User,
    channel: PartnerChannel,
    role: str,
    status: str = MembershipStatus.ACTIVE,
) -> Membership:
    return Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=status,
    )


def _snapshot() -> tuple:
    return (
        list(
            Account.objects.order_by("id").values_list(
                "id",
                "kind",
                "user_id",
                "balance",
                "version",
            )
        ),
        CreditLedgerEntry.objects.count(),
        list(
            CustomerAttribution.objects.order_by("id").values_list(
                "id",
                "user_id",
                "partner_channel_id",
            )
        ),
        list(
            Membership.objects.order_by("id").values_list(
                "id",
                "user_id",
                "organization_id",
                "role",
                "status",
            )
        ),
        list(
            PartnerChannel.objects.order_by("id").values_list(
                "id",
                "kind",
                "owner_user_id",
                "organization_id",
                "is_active",
                "revenue_share_percent",
            )
        ),
    )


def test_individual_owner_sees_own_channel_and_another_user_does_not() -> None:
    owner = _user("owner", display_name="  Ada  ")
    stranger = _user("stranger")
    channel = _individual(owner)

    contexts = list_authorized_partner_contexts(owner)

    assert len(contexts) == 1
    assert contexts[0].channel_id == channel.pk
    assert contexts[0].kind == PartnerChannel.Kind.INDIVIDUAL
    assert contexts[0].label == "Ada"
    assert contexts[0].effective_role == MembershipRole.OWNER
    assert list_authorized_partner_contexts(stranger) == ()
    with pytest.raises(PartnerAccessDenied, match="No partner channel is available"):
        resolve_authorized_partner_context(stranger, channel.pk)


def test_active_team_roles_include_member_for_customers_only() -> None:
    owner = _user("owner")
    admin = _user("admin")
    viewer = _user("viewer")
    member = _user("member")
    channel = _team(owner, "Fleet")
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)

    owner_context = list_authorized_partner_contexts(owner)[0]
    admin_context = list_authorized_partner_contexts(admin)[0]
    viewer_context = list_authorized_partner_contexts(viewer)[0]

    assert owner_context.effective_role == MembershipRole.OWNER
    assert owner_context.label == "Fleet"
    assert admin_context.effective_role == MembershipRole.ADMIN
    assert viewer_context.effective_role == MembershipRole.VIEWER
    member_context = list_authorized_partner_contexts(member)[0]
    assert member_context.effective_role == MembershipRole.MEMBER
    assert partner_role_can_grant(member_context.effective_role) is False
    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(member, channel.pk)
    customers = resolve_authorized_customers_context(member, channel.pk)
    assert customers.effective_role == MembershipRole.MEMBER
    assert customers.channel_id == channel.pk
    assert partner_role_can_grant(owner_context.effective_role) is True
    assert partner_role_can_manage_invite(owner_context.effective_role) is True
    assert partner_role_can_grant(admin_context.effective_role) is True
    assert partner_role_can_manage_invite(admin_context.effective_role) is False
    assert partner_role_can_grant(viewer_context.effective_role) is False
    assert partner_role_can_manage_invite(viewer_context.effective_role) is False


def test_suspended_and_revoked_memberships_are_not_contexts() -> None:
    owner = _user("owner")
    suspended = _user("suspended")
    revoked = _user("revoked")
    channel = _team(owner, "Fleet")
    _membership(
        user=suspended,
        channel=channel,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.SUSPENDED,
    )
    _membership(
        user=revoked,
        channel=channel,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.REVOKED,
    )

    assert list_authorized_partner_contexts(suspended) == ()
    assert list_authorized_partner_contexts(revoked) == ()
    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(suspended, channel.pk)
    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(revoked, channel.pk)


def test_same_user_can_hold_individual_and_several_team_contexts() -> None:
    user = _user("user")
    individual = _individual(user)
    alpha = _team(user, "Alpha")
    beta_owner = _user("beta-owner")
    beta = _team(beta_owner, "Beta")
    _membership(user=user, channel=beta, role=MembershipRole.VIEWER)
    before = _snapshot()

    contexts = list_authorized_partner_contexts(user)

    assert [item.channel_id for item in contexts] == [
        individual.pk,
        alpha.pk,
        beta.pk,
    ]
    assert [item.kind for item in contexts] == [
        PartnerChannel.Kind.INDIVIDUAL,
        PartnerChannel.Kind.TEAM,
        PartnerChannel.Kind.TEAM,
    ]
    assert contexts[2].effective_role == MembershipRole.VIEWER
    assert _snapshot() == before
    with pytest.raises(PartnerContextAmbiguous):
        resolve_partner_summary_channel(user)


def test_multiple_team_contexts_do_not_raise_ambiguity() -> None:
    user = _user("user")
    first_owner = _user("first")
    second_owner = _user("second")
    first = _team(first_owner, "Alpha")
    second = _team(second_owner, "Zeta")
    _membership(user=user, channel=first, role=MembershipRole.ADMIN)
    _membership(user=user, channel=second, role=MembershipRole.VIEWER)

    contexts = list_authorized_partner_contexts(user)

    assert [item.channel_id for item in contexts] == [first.pk, second.pk]
    assert contexts[0].effective_role == MembershipRole.ADMIN
    assert contexts[1].effective_role == MembershipRole.VIEWER


def test_requested_channel_is_returned_without_fallback() -> None:
    user = _user("user")
    individual = _individual(user)
    team = _team(user, "Fleet")
    foreign = _individual(_user("foreign"))
    unknown = uuid.uuid4()

    resolved_individual = resolve_authorized_partner_context(user, individual.pk)
    resolved_team = resolve_authorized_partner_context(user, team.pk)

    assert resolved_individual.channel_id == individual.pk
    assert resolved_individual.effective_role == MembershipRole.OWNER
    assert resolved_team.channel_id == team.pk
    assert resolved_team.kind == PartnerChannel.Kind.TEAM
    for channel_id in (foreign.pk, unknown):
        with pytest.raises(
            PartnerAccessDenied, match="No partner channel is available"
        ):
            resolve_authorized_partner_context(user, channel_id)
    assert [item.channel_id for item in list_authorized_partner_contexts(user)] == [
        individual.pk,
        team.pk,
    ]


def test_removed_access_does_not_fall_back_to_another_channel() -> None:
    user = _user("user")
    individual = _individual(user)
    team = _team(_user("owner"), "Fleet")
    membership = _membership(user=user, channel=team, role=MembershipRole.ADMIN)
    assert resolve_authorized_partner_context(user, team.pk).channel_id == team.pk
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status", "updated_at"])

    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(user, team.pk)

    remaining = resolve_authorized_partner_context(user, individual.pk)
    assert remaining.channel_id == individual.pk
    assert [item.channel_id for item in list_authorized_partner_contexts(user)] == [
        individual.pk
    ]


def test_channel_id_alone_does_not_grant_access() -> None:
    stranger = _user("stranger")
    channel = _team(_user("owner"), "Fleet")

    assert list_authorized_partner_contexts(stranger) == ()
    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(stranger, channel.pk)


def test_access_does_not_imply_grant_or_invite_permission() -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    channel = _team(owner, "Fleet")
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    viewer_context = resolve_authorized_partner_context(viewer, channel.pk)

    assert viewer_context.channel_id == channel.pk
    assert partner_role_can_grant(viewer_context.effective_role) is False
    assert partner_role_can_manage_invite(viewer_context.effective_role) is False
    assert partner_role_can_grant(MembershipRole.MEMBER) is False
    assert partner_role_can_manage_invite(MembershipRole.ADMIN) is False


def test_inactive_channel_stays_accessible_and_roles_ignore_is_active() -> None:
    """ADR 023: is_active stops new accruals only.

    Portal access, grants of existing balance, and invite-link writes stay
    available. The role predicates therefore do not read is_active. A viewer
    can still access the channel and still cannot grant or manage the link.
    """
    owner = _user("owner")
    viewer = _user("viewer")
    channel = _team(owner, "Fleet")
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    channel.is_active = False
    channel.save(update_fields=["is_active"])
    channel.organization.status = OrganizationStatus.SUSPENDED
    channel.organization.save(update_fields=["status"])

    owner_context = resolve_authorized_partner_context(owner, channel.pk)
    viewer_context = resolve_authorized_partner_context(viewer, channel.pk)

    assert owner_context.channel_id == channel.pk
    assert viewer_context.channel_id == channel.pk
    assert partner_role_can_grant(owner_context.effective_role) is True
    assert partner_role_can_manage_invite(owner_context.effective_role) is True
    assert partner_role_can_grant(viewer_context.effective_role) is False
    assert partner_role_can_manage_invite(viewer_context.effective_role) is False
    assert list_authorized_partner_contexts(owner)[0].channel_id == channel.pk


def test_listing_does_not_query_ledger_or_accounts_per_channel() -> None:
    user = _user("user")
    _individual(user)
    _team(user, "Alpha")
    beta = _team(_user("beta"), "Beta")
    gamma = _team(_user("gamma"), "Gamma")
    _membership(user=user, channel=beta, role=MembershipRole.ADMIN)
    _membership(user=user, channel=gamma, role=MembershipRole.VIEWER)

    with CaptureQueriesContext(connection) as listed:
        contexts = list_authorized_partner_contexts(user)

    assert len(contexts) == 4
    listed_sql = " ".join(query["sql"] for query in listed.captured_queries).lower()
    assert len(listed.captured_queries) == 2
    assert "creditledger" not in listed_sql
    assert "partnermargin" not in listed_sql
    assert "billing_account" not in listed_sql

    with CaptureQueriesContext(connection) as one:
        resolve_authorized_partner_context(user, contexts[0].channel_id)
    assert len(one.captured_queries) == 1

    with CaptureQueriesContext(connection) as team:
        resolve_authorized_partner_context(user, contexts[1].channel_id)
    assert len(team.captured_queries) == 2
