"""Transfer ownership HTTP API (ADR 020 / PR11)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.exceptions import NotAllowedError
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization, transfer_ownership

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
def stranger(db):
    return User.objects.create_user(email="stranger@example.com", password=PASSWORD)


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


def _transfer(client, access, org_id, new_owner_user_id: int):
    return client.post(
        f"/api/v1/orgs/{org_id}/transfer-ownership/",
        data=json.dumps({"new_owner_user_id": new_owner_user_id}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


@pytest.mark.django_db
def test_transfer_ownership_post_state(client, owner, org, admin_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    access = _access_token(client, owner.email)
    resp = _transfer(client, access, org.pk, admin_user.pk)
    assert resp.status_code == 200, resp.content
    body = resp.json()

    assert body["new_owner_membership"]["user_id"] == admin_user.pk
    assert body["new_owner_membership"]["role"] == MembershipRole.OWNER
    assert body["organization"]["my_role"] == MembershipRole.ADMIN
    assert body["organization"]["permissions"]["can_transfer_ownership"] is False

    old = Membership.objects.get(organization=org, user=owner)
    new = Membership.objects.get(organization=org, user=admin_user)
    assert old.role == MembershipRole.ADMIN
    assert old.status == MembershipStatus.ACTIVE
    assert new.role == MembershipRole.OWNER
    assert new.status == MembershipStatus.ACTIVE
    assert (
        Membership.objects.filter(
            organization=org,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
        ).count()
        == 1
    )

    # New owner sees transfer permission; former owner does not.
    new_access = _access_token(client, admin_user.email)
    detail = client.get(
        f"/api/v1/orgs/{org.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {new_access}",
    )
    assert detail.status_code == 200
    assert detail.json()["my_role"] == MembershipRole.OWNER
    assert detail.json()["permissions"]["can_transfer_ownership"] is True

    old_detail = client.get(
        f"/api/v1/orgs/{org.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert old_detail.json()["permissions"]["can_transfer_ownership"] is False


@pytest.mark.django_db
def test_transfer_requires_active_member(client, owner, org, stranger):
    access = _access_token(client, owner.email)
    resp = _transfer(client, access, org.pk, stranger.pk)
    assert resp.status_code == 403
    assert (
        Membership.objects.filter(
            organization=org,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_admin_cannot_transfer(client, owner, org, admin_user, member_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    _add_member(org, member_user, MembershipRole.MEMBER)
    access = _access_token(client, admin_user.email)
    resp = _transfer(client, access, org.pk, member_user.pk)
    assert resp.status_code == 403
    assert (
        Membership.objects.get(organization=org, user=owner).role
        == MembershipRole.OWNER
    )


@pytest.mark.django_db
def test_suspended_org_blocks_transfer(client, owner, org, admin_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    access = _access_token(client, owner.email)
    resp = _transfer(client, access, org.pk, admin_user.pk)
    assert resp.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_concurrent_transfer_leaves_one_owner(owner, org, admin_user, member_user):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    _add_member(org, member_user, MembershipRole.MEMBER)

    def _attempt(target_pk: int) -> str:
        try:
            target = User.objects.get(pk=target_pk)
            transfer_ownership(
                actor=owner,
                organization_id=org.pk,
                new_owner=target,
            )
            return "ok"
        except (NotAllowedError, PermissionDenied):
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(_attempt, [admin_user.pk, member_user.pk]),
        )

    assert "ok" in results
    owners = list(
        Membership.objects.filter(
            organization=org,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    assert len(owners) == 1
    assert owners[0].user_id in {admin_user.pk, member_user.pk}
    assert (
        Membership.objects.get(organization=org, user=owner).role
        != MembershipRole.OWNER
    )


@pytest.mark.django_db
def test_transfer_does_not_touch_personal_account_or_esim(
    client, owner, org, admin_user
):
    personal = ensure_billing_account(owner)
    balance = personal.balance
    location = Location.objects.create(
        slug="me-transfer-own",
        title="Montenegro",
        country_code="ME",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    package = Package.objects.create(
        external_id="transfer-own-1gb",
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
        external_order_id="ext-transfer-own",
        customer_ref="rk-transfer-own",
        **product_snapshot_kwargs(package),
    )
    esim = lifecycle_service.create_purchased(
        user=owner,
        account=personal,
        order=order,
        iccid="8900000000000000222",
    )
    _add_member(org, admin_user, MembershipRole.ADMIN)
    access = _access_token(client, owner.email)
    assert _transfer(client, access, org.pk, admin_user.pk).status_code == 200

    personal.refresh_from_db()
    esim.refresh_from_db()
    assert personal.balance == balance
    assert personal.pk != org.account_id
    assert esim.account_id == personal.pk
    assert Esim.objects.filter(account=org.account).count() == 0
