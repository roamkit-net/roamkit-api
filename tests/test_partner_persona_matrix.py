"""Path-scoped partner access across the portal personas (ADR 024)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.billing.services.partner_context import list_authorized_partner_contexts
from apps.billing.services.partner_invite import (
    create_individual_partner_channel,
    create_partner_channel,
)
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _team(actor: User) -> PartnerChannel:
    org = create_organization(name=f"Team {uuid.uuid4()}", actor=actor)
    return create_partner_channel(
        organization=org,
        revenue_share_percent=Decimal("40.00"),
    )


def _join(channel: PartnerChannel, user: User, role: str, status: str) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=status,
    )


def _customer(channel: PartnerChannel, user: User) -> None:
    CustomerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


@ENABLED
@pytest.mark.django_db
def test_persona_reads_follow_portal_roles():
    client = Client()
    individual_owner = _user("individual")
    individual = create_individual_partner_channel(owner=individual_owner)
    team_owner = _user("team-owner")
    team = _team(team_owner)
    second = _team(_user("second-owner"))
    admin = _user("admin")
    viewer = _user("viewer")
    member = _user("member")
    suspended = _user("suspended")
    revoked = _user("revoked")
    ordinary = _user("ordinary")
    _join(team, admin, MembershipRole.ADMIN, MembershipStatus.ACTIVE)
    _join(team, viewer, MembershipRole.VIEWER, MembershipStatus.ACTIVE)
    _join(team, member, MembershipRole.MEMBER, MembershipStatus.ACTIVE)
    _join(team, suspended, MembershipRole.VIEWER, MembershipStatus.SUSPENDED)
    _join(team, revoked, MembershipRole.ADMIN, MembershipStatus.REVOKED)
    both = _user("both")
    create_individual_partner_channel(owner=both)
    _join(second, both, MembershipRole.VIEWER, MembershipStatus.ACTIVE)

    readable = {
        individual_owner: {individual.pk},
        team_owner: {team.pk},
        admin: {team.pk},
        viewer: {team.pk},
        both: {both.individual_partner_channel.pk, second.pk},
    }
    denied = (member, suspended, revoked, ordinary)
    channels = (individual, team, second)

    for user, allowed in readable.items():
        contexts = client.get("/api/v1/partner/contexts/", **_auth(user))
        assert contexts.status_code == 200
        ids = {item["channel_id"] for item in contexts.json()["contexts"]}
        assert ids == {str(channel_id) for channel_id in allowed}
        for channel in channels:
            summary = client.get(
                f"/api/v1/partner/channels/{channel.pk}/summary/",
                **_auth(user),
            )
            expected = 200 if channel.pk in allowed else 403
            assert summary.status_code == expected
            if expected == 403:
                assert summary.json()["code"] == "partner_access_denied"

    unknown = uuid.uuid4()
    missing = client.get(
        f"/api/v1/partner/channels/{unknown}/summary/",
        **_auth(ordinary),
    )
    foreign = client.get(
        f"/api/v1/partner/channels/{team.pk}/summary/",
        **_auth(ordinary),
    )
    assert missing.status_code == foreign.status_code == 403
    assert missing.json() == foreign.json()

    for user in denied:
        assert client.get("/api/v1/partner/contexts/", **_auth(user)).json() == {
            "contexts": []
        }
        denied_summary = client.get(
            f"/api/v1/partner/channels/{team.pk}/summary/",
            **_auth(user),
        )
        assert denied_summary.status_code == 403
        assert denied_summary.json()["code"] == "partner_access_denied"


@ENABLED
@pytest.mark.django_db
def test_customers_and_invite_do_not_cross_channels():
    client = Client()
    owner_a = _user("a")
    channel_a = create_individual_partner_channel(owner=owner_a)
    owner_b = _user("b")
    channel_b = _team(owner_b)
    customer_a = _user("customer-a")
    customer_b = _user("customer-b")
    _customer(channel_a, customer_a)
    _customer(channel_b, customer_b)

    page_a = client.get(
        f"/api/v1/partner/channels/{channel_a.pk}/customers/",
        **_auth(owner_a),
    )
    page_b = client.get(
        f"/api/v1/partner/channels/{channel_b.pk}/customers/",
        **_auth(owner_b),
    )
    assert page_a.status_code == page_b.status_code == 200
    ids_a = {row["customer_id"] for row in page_a.json()["results"]}
    ids_b = {row["customer_id"] for row in page_b.json()["results"]}
    assert ids_a == {customer_a.pk}
    assert ids_b == {customer_b.pk}

    invite_a = client.get(
        f"/api/v1/partner/channels/{channel_a.pk}/invite-link/",
        **_auth(owner_a),
    )
    invite_b = client.get(
        f"/api/v1/partner/channels/{channel_b.pk}/invite-link/",
        **_auth(owner_b),
    )
    assert invite_a.json()["url"] != invite_b.json()["url"]
    hidden = client.get(
        f"/api/v1/partner/channels/{channel_b.pk}/invite-link/",
        **_auth(owner_a),
    )
    assert hidden.status_code == 403


@ENABLED
@pytest.mark.django_db
def test_viewer_cannot_grant_or_manage_invite_and_inactive_stays_readable():
    client = Client()
    channel = _team(_user("founder"))
    viewer = _user("viewer")
    _join(channel, viewer, MembershipRole.VIEWER, MembershipStatus.ACTIVE)
    channel.is_active = False
    channel.save(update_fields=["is_active", "updated_at"])

    summary = client.get(
        f"/api/v1/partner/channels/{channel.pk}/summary/",
        **_auth(viewer),
    )
    assert summary.status_code == 200
    grant = client.post(
        f"/api/v1/partner/channels/{channel.pk}/grants/",
        data="{}",
        content_type="application/json",
        **_auth(viewer),
    )
    assert grant.status_code == 403
    assert grant.json()["code"] == "partner_grant_forbidden"
    invite = client.post(
        f"/api/v1/partner/channels/{channel.pk}/invite-link/regenerate/",
        **_auth(viewer),
    )
    assert invite.status_code == 403
    assert invite.json()["code"] == "partner_invite_forbidden"


@ENABLED
@pytest.mark.django_db
def test_context_list_stays_bounded_for_two_teams():
    owner = _user("owner")
    first = _team(owner)
    second = _team(_user("other"))
    _join(second, owner, MembershipRole.ADMIN, MembershipStatus.ACTIVE)
    create_individual_partner_channel(owner=owner)
    assert first.pk != second.pk
    with CaptureQueriesContext(connection) as captured:
        contexts = list_authorized_partner_contexts(owner)
    assert len(contexts) == 3
    assert len(captured) <= 8
