"""Organization invites (ADR 020 / PR5)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.exceptions import (
    InviteConflictError,
    InviteInvalidError,
    NotAllowedError,
)
from apps.organizations.models import (
    InviteRole,
    InviteStatus,
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationInvite,
    OrganizationStatus,
)
from apps.organizations.services import (
    accept_invite,
    create_invite,
    create_organization,
    revoke_invite,
)
from apps.organizations.services.invites import _hash_token

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password=PASSWORD)


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(email="admin@example.com", password=PASSWORD)


@pytest.fixture
def member_user(db):
    return User.objects.create_user(email="member@example.com", password=PASSWORD)


@pytest.fixture
def invitee(db):
    return User.objects.create_user(email="invitee@example.com", password=PASSWORD)


@pytest.fixture
def stranger(db):
    return User.objects.create_user(email="stranger@example.com", password=PASSWORD)


def _add_member(org, user, role: str) -> Membership:
    return Membership.objects.create(
        organization=org,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


@pytest.fixture
def org(owner):
    organization = create_organization(name="Fleet Ops")
    _add_member(organization, owner, MembershipRole.OWNER)
    return organization


def _access_token(client: Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["access"]


# --- create / duplicate pending ---


@pytest.mark.django_db
def test_create_invite_upserts_pending_same_email(org, owner):
    first = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="  New.User@Example.COM ",
        role=InviteRole.MEMBER,
    )
    assert first.created is True
    assert first.invite.email_normalized == "new.user@example.com"
    assert first.invite.status == InviteStatus.PENDING

    second = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="new.user@example.com",
        role=InviteRole.ADMIN,
    )
    assert second.created is False
    assert second.invite.pk == first.invite.pk
    assert second.invite.role == InviteRole.ADMIN
    assert second.raw_token != first.raw_token
    assert (
        OrganizationInvite.objects.filter(
            organization=org,
            status=InviteStatus.PENDING,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_create_invite_after_revoke_allows_new_row(org, owner):
    first = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="again@example.com",
    )
    revoke_invite(actor=owner, organization_id=org.pk, invite_id=first.invite.pk)
    second = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="again@example.com",
    )
    assert second.created is True
    assert second.invite.pk != first.invite.pk
    assert first.invite.pk  # still exists as revoked
    first.invite.refresh_from_db()
    assert first.invite.status == InviteStatus.REVOKED


@pytest.mark.django_db
def test_create_invite_rejects_active_member(org, owner, member_user):
    _add_member(org, member_user, MembershipRole.MEMBER)
    with pytest.raises(InviteConflictError):
        create_invite(
            actor=owner,
            organization_id=org.pk,
            email=member_user.email,
        )


@pytest.mark.django_db
def test_create_invite_rejects_owner_role(org, owner):
    with pytest.raises(NotAllowedError):
        create_invite(
            actor=owner,
            organization_id=org.pk,
            email="x@example.com",
            role=MembershipRole.OWNER,
        )


@pytest.mark.django_db
def test_member_cannot_create_invite(org, owner, member_user):
    _add_member(org, member_user, MembershipRole.MEMBER)
    from rest_framework.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        create_invite(
            actor=member_user,
            organization_id=org.pk,
            email="x@example.com",
        )


@pytest.mark.django_db
def test_suspended_org_cannot_receive_invite(org, owner):
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    from rest_framework.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        create_invite(
            actor=owner,
            organization_id=org.pk,
            email="x@example.com",
        )


# --- accept ---


@pytest.mark.django_db
def test_accept_creates_membership_and_is_idempotent(org, owner, invitee):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
        role=InviteRole.VIEWER,
    )
    first = accept_invite(actor=invitee, raw_token=created.raw_token)
    assert first.already_accepted is False
    assert first.membership.role == MembershipRole.VIEWER
    assert first.membership.status == MembershipStatus.ACTIVE

    second = accept_invite(actor=invitee, raw_token=created.raw_token)
    assert second.already_accepted is True
    assert second.membership.pk == first.membership.pk
    assert Membership.objects.filter(organization=org, user=invitee).count() == 1


@pytest.mark.django_db
def test_accept_rejects_email_mismatch(org, owner, invitee, stranger):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
    )
    with pytest.raises(InviteInvalidError):
        accept_invite(actor=stranger, raw_token=created.raw_token)


@pytest.mark.django_db
def test_accept_rejects_revoked(org, owner, invitee):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
    )
    revoke_invite(actor=owner, organization_id=org.pk, invite_id=created.invite.pk)
    with pytest.raises(InviteInvalidError):
        accept_invite(actor=invitee, raw_token=created.raw_token)


@pytest.mark.django_db
def test_accept_rejects_expired(org, owner, invitee):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
    )
    invite = created.invite
    invite.expires_at = timezone.now() - timedelta(seconds=1)
    invite.save(update_fields=["expires_at", "updated_at"])
    with pytest.raises(InviteInvalidError):
        accept_invite(actor=invitee, raw_token=created.raw_token)
    invite.refresh_from_db()
    assert invite.status == InviteStatus.EXPIRED


@pytest.mark.django_db(transaction=True)
def test_concurrent_accept_same_token_one_membership(org, owner, invitee):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
        role=InviteRole.MEMBER,
    )
    token = created.raw_token

    def _accept() -> tuple[bool, str]:
        try:
            result = accept_invite(actor=invitee, raw_token=token)
            return result.already_accepted, str(result.membership.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _accept(), range(2)))

    already_flags = sorted(flag for flag, _ in outcomes)
    membership_ids = {mid for _, mid in outcomes}
    assert len(membership_ids) == 1
    assert already_flags == [False, True]
    assert Membership.objects.filter(organization=org, user=invitee).count() == 1
    invite = OrganizationInvite.objects.get(pk=created.invite.pk)
    assert invite.status == InviteStatus.ACCEPTED
    assert invite.accepted_by_id == invitee.pk


# --- wallet / inventory isolation ---


@pytest.mark.django_db
def test_accept_does_not_touch_wallets_or_esim_inventory(org, owner, invitee):
    personal = ensure_billing_account(invitee)
    personal_balance = personal.balance
    team_balance = org.account.balance

    location = Location.objects.create(
        slug="montenegro-invite",
        title="Montenegro",
        country_code="ME",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    package = Package.objects.create(
        external_id="invite-iso-1gb",
        title="1 GB",
        operator_title="Jezero",
        country_code="ME",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        net_price_usd=Decimal("2.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=personal,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-invite-iso",
        customer_ref="rk-invite-iso",
        **product_snapshot_kwargs(package),
    )
    esim = lifecycle_service.create_purchased(
        user=invitee,
        account=personal,
        order=order,
        iccid="8900000000000000001",
        matching_id="match-invite",
    )
    assert esim.account_id == personal.pk

    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email=invitee.email,
    )
    accept_invite(actor=invitee, raw_token=created.raw_token)

    personal.refresh_from_db()
    org.account.refresh_from_db()
    esim.refresh_from_db()
    assert personal.balance == personal_balance
    assert org.account.balance == team_balance
    assert esim.account_id == personal.pk
    assert esim.user_id == invitee.pk
    assert Esim.objects.filter(account=org.account).count() == 0


# --- HTTP ---


@pytest.mark.django_db
def test_api_create_list_revoke_accept(client, org, owner, admin_user, invitee):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    access = _access_token(client, owner.email)

    create_resp = client.post(
        f"/api/v1/orgs/{org.pk}/invites/",
        data=json.dumps({"email": invitee.email, "role": "member"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert create_resp.status_code == 201, create_resp.content
    body = create_resp.json()
    token = body["token"]
    invite_id = body["invite"]["id"]
    assert body["created"] is True
    assert "token_hash" not in body["invite"]

    list_resp = client.get(
        f"/api/v1/orgs/{org.pk}/invites/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # Refresh upsert returns 200
    refresh = client.post(
        f"/api/v1/orgs/{org.pk}/invites/",
        data=json.dumps({"email": invitee.email.upper(), "role": "viewer"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_access_token(client, admin_user.email)}",
    )
    assert refresh.status_code == 200, refresh.content
    assert refresh.json()["created"] is False
    token = refresh.json()["token"]

    invitee_access = _access_token(client, invitee.email)
    accept_resp = client.post(
        "/api/v1/orgs/invites/accept/",
        data=json.dumps({"token": token}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {invitee_access}",
    )
    assert accept_resp.status_code == 200, accept_resp.content
    assert accept_resp.json()["already_accepted"] is False
    assert accept_resp.json()["organization_id"] == str(org.pk)

    # Old invite id still exists as accepted — revoke of non-pending is idempotent ok
    # Create another invite then revoke
    other = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="other@example.com",
    )
    revoke_resp = client.post(
        f"/api/v1/orgs/{org.pk}/invites/{other.invite.pk}/revoke/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == InviteStatus.REVOKED
    assert invite_id  # used above


@pytest.mark.django_db
def test_api_member_cannot_list_invites(client, org, owner, member_user):
    _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, member_user.email)
    resp = client.get(
        f"/api/v1/orgs/{org.pk}/invites/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_token_hash_not_raw(org, owner):
    created = create_invite(
        actor=owner,
        organization_id=org.pk,
        email="hash@example.com",
    )
    created.invite.refresh_from_db()
    assert created.invite.token_hash == _hash_token(created.raw_token)
    assert created.raw_token not in created.invite.token_hash
