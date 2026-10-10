"""Path-scoped grant history and invite link (ADR 024)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.services.partner_invite import (
    create_individual_partner_channel,
    create_partner_channel,
)
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


@pytest.fixture
def client() -> Client:
    return Client()


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _team(actor: User):
    org = create_organization(name=f"Team {uuid.uuid4()}", actor=actor)
    return create_partner_channel(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
    )


def _member(channel, user: User, role: str) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


@ENABLED
@pytest.mark.django_db
def test_individual_owner_reads_and_regenerates_invite(client: Client):
    owner = _user("owner")
    channel = create_individual_partner_channel(owner=owner)
    url = f"/api/v1/partner/channels/{channel.pk}/invite-link/"

    read = client.get(url, **_auth(owner))
    assert read.status_code == 200
    assert read["Cache-Control"] == "no-store"
    assert read["X-Partner-Role"] == "owner"
    assert read.json()["url"]

    regenerated = client.post(f"{url}regenerate/", **_auth(owner))
    assert regenerated.status_code == 200
    assert regenerated.json()["url"] != read.json()["url"]


@ENABLED
@pytest.mark.django_db
def test_viewer_reads_invite_and_cannot_write(client: Client):
    channel = _team(_user("founder"))
    viewer = _user("viewer")
    _member(channel, viewer, MembershipRole.VIEWER)
    url = f"/api/v1/partner/channels/{channel.pk}/invite-link/"

    read = client.get(url, **_auth(viewer))
    assert read.status_code == 200
    assert read["X-Partner-Role"] == "viewer"

    denied = client.post(f"{url}regenerate/", **_auth(viewer))
    assert denied.status_code == 403
    assert denied.json()["code"] == "partner_invite_forbidden"


@ENABLED
@pytest.mark.django_db
def test_admin_cannot_manage_invite(client: Client):
    channel = _team(_user("founder"))
    admin = _user("admin")
    _member(channel, admin, MembershipRole.ADMIN)

    denied = client.post(
        f"/api/v1/partner/channels/{channel.pk}/invite-link/deactivate/",
        **_auth(admin),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "partner_invite_forbidden"


@ENABLED
@pytest.mark.django_db
def test_grant_history_is_scoped_to_the_path_channel(client: Client):
    owner = _user("owner")
    own = create_individual_partner_channel(owner=owner)
    other = _team(_user("founder"))
    url = f"/api/v1/partner/channels/{own.pk}/grants/"

    page = client.get(url, **_auth(owner))
    assert page.status_code == 200
    assert page["Cache-Control"] == "no-store"
    assert page["X-Partner-Role"] == "owner"
    assert page.json()["results"] == []

    hidden = client.get(f"/api/v1/partner/channels/{other.pk}/grants/", **_auth(owner))
    assert hidden.status_code == 403
    assert hidden.json()["code"] == "partner_access_denied"


@ENABLED
@pytest.mark.django_db
def test_inaccessible_grant_history_ignores_a_bad_query(client: Client):
    owner = _user("owner")
    response = client.get(
        f"/api/v1/partner/channels/{uuid.uuid4()}/grants/?page=no",
        **_auth(owner),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "partner_access_denied"


@pytest.mark.django_db
def test_channel_reads_are_404_when_the_flag_is_off(client: Client):
    owner = _user("owner")
    channel = create_individual_partner_channel(owner=owner)
    with override_settings(PARTNER_CHANNEL_ENABLED=False):
        response = client.get(
            f"/api/v1/partner/channels/{channel.pk}/invite-link/",
            **_auth(owner),
        )
    assert response.status_code == 404
    assert response.json()["code"] == "partner_channel_disabled"
    assert "url" not in response.json()
