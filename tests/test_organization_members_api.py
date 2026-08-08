"""Membership manage HTTP API (ADR 020 / PR9)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization

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
def viewer_user(db):
    return User.objects.create_user(email="viewer@example.com", password=PASSWORD)


def _access_token(client: Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["access"]


def _add_member(org, user, role: str) -> Membership:
    return Membership.objects.create(
        organization=org,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Fleet Ops", actor=owner)


def _patch_role(client, access, org_id, membership_id, role: str):
    return client.patch(
        f"/api/v1/orgs/{org_id}/members/{membership_id}/",
        data=json.dumps({"role": role}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


def _revoke(client, access, org_id, membership_id):
    return client.post(
        f"/api/v1/orgs/{org_id}/members/{membership_id}/revoke/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


@pytest.mark.django_db
def test_owner_can_set_member_role(client, owner, org, member_user):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, owner.email)
    resp = _patch_role(client, access, org.pk, membership.pk, "admin")
    assert resp.status_code == 200, resp.content
    assert resp.json()["role"] == MembershipRole.ADMIN
    membership.refresh_from_db()
    assert membership.role == MembershipRole.ADMIN


@pytest.mark.django_db
def test_admin_can_set_member_role(client, owner, org, admin_user, member_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, admin_user.email)
    resp = _patch_role(client, access, org.pk, membership.pk, "viewer")
    assert resp.status_code == 200, resp.content
    assert resp.json()["role"] == MembershipRole.VIEWER


@pytest.mark.django_db
def test_member_and_viewer_cannot_manage(client, owner, org, member_user, viewer_user):
    member_m = _add_member(org, member_user, MembershipRole.MEMBER)
    viewer_m = _add_member(org, viewer_user, MembershipRole.VIEWER)

    member_access = _access_token(client, member_user.email)
    resp = _patch_role(client, member_access, org.pk, viewer_m.pk, "member")
    assert resp.status_code == 403

    viewer_access = _access_token(client, viewer_user.email)
    resp = _revoke(client, viewer_access, org.pk, member_m.pk)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_cannot_assign_owner_via_patch(client, owner, org, member_user):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, owner.email)
    resp = _patch_role(client, access, org.pk, membership.pk, "owner")
    assert resp.status_code == 400
    membership.refresh_from_db()
    assert membership.role == MembershipRole.MEMBER


@pytest.mark.django_db
def test_cannot_change_owner_role(client, owner, org, admin_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    owner_m = Membership.objects.get(organization=org, user=owner)
    access = _access_token(client, admin_user.email)
    resp = _patch_role(client, access, org.pk, owner_m.pk, "admin")
    assert resp.status_code == 403
    owner_m.refresh_from_db()
    assert owner_m.role == MembershipRole.OWNER


@pytest.mark.django_db
def test_admin_cannot_promote_self_to_owner(client, owner, org, admin_user):
    """Ownership invariant: owner role is not assignable via set_member_role."""
    admin_m = _add_member(org, admin_user, MembershipRole.ADMIN)
    access = _access_token(client, admin_user.email)
    resp = _patch_role(client, access, org.pk, admin_m.pk, "owner")
    assert resp.status_code == 400
    admin_m.refresh_from_db()
    assert admin_m.role == MembershipRole.ADMIN


@pytest.mark.django_db
def test_admin_can_demote_self_without_touching_owner(client, owner, org, admin_user):
    """Self-demote is allowed; sole owner membership stays intact."""
    admin_m = _add_member(org, admin_user, MembershipRole.ADMIN)
    owner_m = Membership.objects.get(organization=org, user=owner)
    access = _access_token(client, admin_user.email)
    resp = _patch_role(client, access, org.pk, admin_m.pk, "member")
    assert resp.status_code == 200, resp.content
    admin_m.refresh_from_db()
    owner_m.refresh_from_db()
    assert admin_m.role == MembershipRole.MEMBER
    assert owner_m.role == MembershipRole.OWNER
    assert owner_m.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
def test_cannot_revoke_sole_owner(client, owner, org):
    owner_m = Membership.objects.get(organization=org, user=owner)
    access = _access_token(client, owner.email)
    resp = _revoke(client, access, org.pk, owner_m.pk)
    assert resp.status_code == 403
    owner_m.refresh_from_db()
    assert owner_m.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
def test_owner_can_revoke_member(client, owner, org, member_user):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, owner.email)
    resp = _revoke(client, access, org.pk, membership.pk)
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == MembershipStatus.REVOKED
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED


@pytest.mark.django_db
def test_suspended_org_blocks_mutations(client, owner, org, member_user):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    access = _access_token(client, owner.email)
    resp = _patch_role(client, access, org.pk, membership.pk, "admin")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_manage_does_not_touch_personal_account_or_esim(
    client, owner, org, member_user
):
    personal = ensure_billing_account(member_user)
    personal_balance = personal.balance
    location = Location.objects.create(
        slug="me-members-api",
        title="Montenegro",
        country_code="ME",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    package = Package.objects.create(
        external_id="members-api-1gb",
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
        external_order_id="ext-members-api",
        customer_ref="rk-members-api",
        **product_snapshot_kwargs(package),
    )
    esim = lifecycle_service.create_purchased(
        user=member_user,
        account=personal,
        order=order,
        iccid="8900000000000000111",
    )
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, owner.email)

    patch_resp = _patch_role(client, access, org.pk, membership.pk, "viewer")
    assert patch_resp.status_code == 200
    revoke_resp = _revoke(client, access, org.pk, membership.pk)
    assert revoke_resp.status_code == 200

    personal.refresh_from_db()
    esim.refresh_from_db()
    assert personal.balance == personal_balance
    assert personal.pk != org.account_id
    assert esim.account_id == personal.pk
    assert Esim.objects.filter(account=org.account).count() == 0


@pytest.mark.django_db
def test_unknown_membership_is_404(client, owner, org):
    access = _access_token(client, owner.email)
    fake_id = "11111111-1111-1111-1111-111111111111"
    resp = _patch_role(client, access, org.pk, fake_id, "member")
    assert resp.status_code == 404
