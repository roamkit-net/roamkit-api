"""GET /api/v1/partner/contexts/ (ADR 024)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import Account, CreditLedgerEntry
from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/partner/contexts/"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


@pytest.fixture
def client() -> Client:
    return Client()


def _user(prefix: str, *, display_name: str = "") -> User:
    user = User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    if display_name:
        user.display_name = display_name
        user.save(update_fields=["display_name"])
    return user


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _get(client: Client, user: User | None):
    headers = _auth(user) if user is not None else {}
    return client.get(URL, **headers)


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
                "balance",
                "kind",
                "user_id",
            )
        ),
        CreditLedgerEntry.objects.count(),
        CustomerAttribution.objects.count(),
        list(
            Membership.objects.order_by("id").values_list(
                "id",
                "role",
                "status",
                "user_id",
                "organization_id",
            )
        ),
        list(
            PartnerChannel.objects.order_by("id").values_list(
                "id",
                "kind",
                "owner_user_id",
                "organization_id",
                "is_active",
            )
        ),
    )


def _keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(value)
        for child in value.values():
            found |= _keys(child)
    elif isinstance(value, list):
        for child in value:
            found |= _keys(child)
    return found


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required(client: Client) -> None:
    response = _get(client, None)

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"


@ENABLED
@pytest.mark.django_db
def test_flag_off_is_disabled(client: Client, settings) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    _individual(owner)

    response = _get(client, owner)

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_user_with_no_context_gets_an_empty_list(client: Client) -> None:
    user = _user("user")

    response = _get(client, user)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response
    assert response.json() == {"contexts": []}


@ENABLED
@pytest.mark.django_db
def test_lists_individual_and_team_contexts_without_financial_fields(
    client: Client,
) -> None:
    user = _user("user", display_name="  Ada  ")
    individual = _individual(user)
    alpha = _team(user, "Alpha")
    beta = _team(_user("beta-owner"), "Beta")
    _membership(user=user, channel=beta, role=MembershipRole.VIEWER)
    before = _snapshot()

    response = _get(client, user)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response
    body = response.json()
    assert [item["channel_id"] for item in body["contexts"]] == [
        str(individual.pk),
        str(alpha.pk),
        str(beta.pk),
    ]
    assert body["contexts"][0] == {
        "channel_id": str(individual.pk),
        "kind": "individual",
        "label": "Ada",
        "effective_role": "owner",
        "is_active": True,
        "capabilities": {"can_grant": True, "can_manage_invite": True},
    }
    assert body["contexts"][1]["label"] == "Alpha"
    assert body["contexts"][1]["effective_role"] == "owner"
    assert body["contexts"][2]["effective_role"] == "viewer"
    assert body["contexts"][2]["capabilities"] == {
        "can_grant": False,
        "can_manage_invite": False,
    }
    leaked = _keys(body) & {
        "account",
        "account_id",
        "balance",
        "available_balance",
        "organization_id",
        "owner_user_id",
        "user_id",
        "membership_id",
    }
    assert leaked == set()
    assert str(user.billing_account.pk) not in response.content.decode()
    assert str(alpha.organization.account_id) not in response.content.decode()
    assert _snapshot() == before


@ENABLED
@pytest.mark.django_db
def test_role_matrix_and_inactive_channel_stay_visible(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    viewer = _user("viewer")
    member = _user("member")
    suspended = _user("suspended")
    revoked = _user("revoked")
    channel = _team(owner, "Fleet")
    channel.is_active = False
    channel.save(update_fields=["is_active"])
    channel.organization.status = OrganizationStatus.SUSPENDED
    channel.organization.save(update_fields=["status"])
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
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

    owner_body = _get(client, owner).json()["contexts"]
    admin_body = _get(client, admin).json()["contexts"]
    viewer_body = _get(client, viewer).json()["contexts"]

    assert owner_body[0]["is_active"] is False
    assert owner_body[0]["capabilities"] == {
        "can_grant": True,
        "can_manage_invite": True,
    }
    assert admin_body[0]["effective_role"] == "admin"
    assert admin_body[0]["capabilities"] == {
        "can_grant": True,
        "can_manage_invite": False,
    }
    assert viewer_body[0]["effective_role"] == "viewer"
    assert viewer_body[0]["capabilities"] == {
        "can_grant": False,
        "can_manage_invite": False,
    }
    assert _get(client, member).json() == {"contexts": []}
    assert _get(client, suspended).json() == {"contexts": []}
    assert _get(client, revoked).json() == {"contexts": []}
