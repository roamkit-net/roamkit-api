"""Invite link, join signature, pending attribution, and consume (ADR 023)."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.accounts.services.email import uid_for_user
from apps.accounts.services.registration import activate_user, register_user
from apps.accounts.tokens import account_activation_token
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_attribution import consume_partner_pending
from apps.billing.services.partner_invite import (
    create_partner_channel,
    issue_join_signature,
    regenerate_invite_link,
)
from apps.billing.services.partner_pending import unsign_partner_pending
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    BILLING_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)
PASSWORD = "SecurePass1!"


def _user(prefix: str, *, active: bool = True) -> User:
    user = User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    if not active:
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["password", "is_active", "updated_at"])
    return user


def _channel(actor: User, *, active: bool = True):
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    channel = create_partner_channel(organization=org)
    if not active:
        channel.is_active = False
        channel.save(update_fields=["is_active", "updated_at"])
    return channel


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _role(channel, user: User, role: str) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


@ENABLED
@pytest.mark.django_db
def test_owner_reads_and_regenerates_one_link(caplog, client: Client) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    old = channel.invite_link.token
    pending_user = _user("pending", active=False)
    PendingPartnerAttribution.objects.create(
        user=pending_user,
        partner_channel=channel,
        invite_token_snapshot=old,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    read = client.get("/api/v1/orgs/partner/invite-link/", **_auth(owner))
    assert read.status_code == 200
    assert read["Cache-Control"] == "no-store"
    assert set(read.json()) == {"url", "is_active", "created_at", "regenerated_at"}
    assert read.json()["url"] == f"https://roamkit.net/join/{old}"
    assert read.json()["regenerated_at"] is None
    assert read["X-Partner-Role"] == "owner"

    with caplog.at_level(logging.INFO):
        regenerated = client.post(
            "/api/v1/orgs/partner/invite-link/regenerate/",
            **_auth(owner),
        )
    assert regenerated.status_code == 200
    assert "X-Partner-Role" not in regenerated
    assert regenerated.json()["url"] != read.json()["url"]
    assert issue_join_signature(old) is None
    assert not PendingPartnerAttribution.objects.filter(user=pending_user).exists()
    assert PartnerInviteLink.objects.filter(partner_channel=channel).count() == 1
    assert old not in caplog.text
    assert "partner_invite.regenerated" in caplog.text


@ENABLED
@pytest.mark.django_db
def test_viewer_reads_and_cannot_mutate(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    admin = _user("admin")
    channel = _channel(owner)
    _role(channel, viewer, MembershipRole.VIEWER)
    _role(channel, admin, MembershipRole.ADMIN)

    read = client.get(
        "/api/v1/orgs/partner/invite-link/",
        HTTP_X_PARTNER_ROLE="owner",
        **_auth(viewer),
    )
    assert read.status_code == 200
    assert read["X-Partner-Role"] == "viewer"
    denied = client.post(
        "/api/v1/orgs/partner/invite-link/deactivate/",
        HTTP_X_PARTNER_ROLE="owner",
        **_auth(viewer),
    )
    admin_denied = client.post(
        "/api/v1/orgs/partner/invite-link/activate/",
        **_auth(admin),
    )
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_invite_forbidden"}
    assert "X-Partner-Role" not in denied
    assert admin_denied.json() == {"code": "partner_invite_forbidden"}
    assert channel.invite_link.is_active is True


@ENABLED
@pytest.mark.django_db
def test_repeat_deactivate_does_not_audit(caplog, client: Client) -> None:
    owner = _user("owner")
    _channel(owner)
    client.post("/api/v1/orgs/partner/invite-link/deactivate/", **_auth(owner))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = client.post(
            "/api/v1/orgs/partner/invite-link/deactivate/",
            **_auth(owner),
        )
    assert again.status_code == 200
    assert "X-Partner-Role" not in again
    assert again.json()["is_active"] is False
    assert "partner_invite.deactivated" not in caplog.text


@pytest.mark.django_db
def test_flag_off_hides_invite_and_join(client: Client) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    token = channel.invite_link.token

    response = client.get("/api/v1/orgs/partner/invite-link/", **_auth(owner))
    signed = client.post(
        "/api/internal/partner/join-sign/",
        data={"token": token},
        content_type="application/json",
    )

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert signed.status_code == 404
    assert signed.content == b""


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_signs_and_inactive_link_does_not() -> None:
    owner = _user("owner")
    channel = _channel(owner, active=False)
    token = channel.invite_link.token
    assert issue_join_signature(token) is not None
    channel.invite_link.is_active = False
    channel.invite_link.save(update_fields=["is_active"])
    assert issue_join_signature(token) is None
    assert issue_join_signature("missing") is None


@ENABLED
@pytest.mark.django_db
def test_register_activation_creates_attribution() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    signed = issue_join_signature(channel.invite_link.token)
    email = f"new-{uuid.uuid4()}@example.com"
    register_user(email=email, partner_pending=signed)
    user = User.objects.get(email=email)
    assert PendingPartnerAttribution.objects.filter(user=user).exists()

    activate_user(
        uid=uid_for_user(user),
        token=account_activation_token.make_token(user),
        password=PASSWORD,
        password_confirm=PASSWORD,
    )
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.partner_channel_id == channel.pk
    assert attribution.source == CustomerAttribution.Source.INVITE_LINK
    assert not PendingPartnerAttribution.objects.filter(user=user).exists()


@ENABLED
@pytest.mark.django_db
def test_existing_user_consume_and_second_partner_is_noop() -> None:
    owner = _user("owner")
    other_owner = _user("other")
    customer = _user("customer")
    channel = _channel(owner)
    other = _channel(other_owner)
    signed = issue_join_signature(channel.invite_link.token)

    assert consume_partner_pending(customer, signed) == "created"
    assert (
        CustomerAttribution.objects.get(user=customer).partner_channel_id == channel.pk
    )

    other_signed = issue_join_signature(other.invite_link.token)
    assert consume_partner_pending(customer, other_signed) == "noop"
    assert (
        CustomerAttribution.objects.get(user=customer).partner_channel_id == channel.pk
    )


@ENABLED
@pytest.mark.django_db
def test_old_token_consume_is_ignored() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    signed = issue_join_signature(channel.invite_link.token)
    regenerate_invite_link(channel, actor=owner, request_id="req")
    assert unsign_partner_pending(signed) is not None
    assert consume_partner_pending(customer, signed) == "ignored"
    assert not CustomerAttribution.objects.filter(user=customer).exists()


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_parallel_consumes_create_one_attribution() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    signed = issue_join_signature(channel.invite_link.token)

    def once(_: int) -> str:
        connection.close()
        return consume_partner_pending(customer, signed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, range(2)))

    assert sorted(results) == ["created", "noop"]
    assert CustomerAttribution.objects.filter(user=customer).count() == 1


@ENABLED
@pytest.mark.django_db
def test_cleanup_deletes_only_expired_pending() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    expired_user = _user("expired", active=False)
    fresh_user = _user("fresh", active=False)
    attributed = _user("kept")
    CustomerAttribution.objects.create(
        user=attributed,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    PendingPartnerAttribution.objects.create(
        user=expired_user,
        partner_channel=channel,
        invite_token_snapshot=channel.invite_link.token,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    PendingPartnerAttribution.objects.create(
        user=fresh_user,
        partner_channel=channel,
        invite_token_snapshot=channel.invite_link.token,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    call_command("cleanup_expired_partner_attributions")

    assert not PendingPartnerAttribution.objects.filter(user=expired_user).exists()
    assert PendingPartnerAttribution.objects.filter(user=fresh_user).exists()
    assert CustomerAttribution.objects.filter(user=attributed).exists()
