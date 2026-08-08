"""Create organization API + atomic service (ADR 020 / PR7)."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.billing.models import Account, AccountKind
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
    Organization,
    OrganizationStatus,
)
from apps.organizations.permissions import permissions_for_role
from apps.organizations.services import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="creator@example.com", password=PASSWORD)


@pytest.fixture
def other(db):
    return User.objects.create_user(email="other@example.com", password=PASSWORD)


def _access_token(client: Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["access"]


@pytest.mark.django_db
def test_create_organization_sets_owner_membership_and_permissions(user):
    org = create_organization(name="  Fleet Ops  ", actor=user)
    assert org.status == OrganizationStatus.ACTIVE
    assert org.name == "Fleet Ops"
    assert org.account.kind == AccountKind.ORGANIZATION
    assert org.account.user_id is None
    assert org.account.balance == Decimal("0")

    membership = Membership.objects.get(organization=org, user=user)
    assert membership.role == MembershipRole.OWNER
    assert membership.status == MembershipStatus.ACTIVE

    perms = permissions_for_role(membership.role)
    assert perms.can_invite is True
    assert perms.can_transfer_ownership is True


@pytest.mark.django_db
def test_create_organization_atomic_rolls_back_on_membership_failure(user):
    before_orgs = Organization.objects.count()
    before_team_accounts = Account.objects.filter(kind=AccountKind.ORGANIZATION).count()
    before_memberships = Membership.objects.count()

    with patch(
        "apps.organizations.services.account_binding.Membership.objects.create",
        side_effect=RuntimeError("membership boom"),
    ):
        with pytest.raises(RuntimeError, match="membership boom"):
            create_organization(name="Doomed", actor=user)

    assert Organization.objects.count() == before_orgs
    assert (
        Account.objects.filter(kind=AccountKind.ORGANIZATION).count()
        == before_team_accounts
    )
    assert Membership.objects.count() == before_memberships


@pytest.mark.django_db
def test_create_does_not_touch_personal_account_or_esims(user):
    personal = ensure_billing_account(user)
    personal_balance = personal.balance

    location = Location.objects.create(
        slug="montenegro-create-org",
        title="Montenegro",
        country_code="ME",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    package = Package.objects.create(
        external_id="create-org-1gb",
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
        external_order_id="ext-create-org",
        customer_ref="rk-create-org",
        **product_snapshot_kwargs(package),
    )
    esim = lifecycle_service.create_purchased(
        user=user,
        account=personal,
        order=order,
        iccid="8900000000000000099",
    )

    org = create_organization(name="Fleet", actor=user)

    personal.refresh_from_db()
    esim.refresh_from_db()
    assert personal.pk != org.account_id
    assert personal.balance == personal_balance
    assert personal.kind == AccountKind.PERSONAL
    assert esim.account_id == personal.pk
    assert Esim.objects.filter(account=org.account).count() == 0


@pytest.mark.django_db
def test_create_preserves_cross_org_isolation(user, other):
    org_a = create_organization(name="Org A", actor=user)
    org_b = create_organization(name="Org B", actor=other)
    assert org_a.account_id != org_b.account_id
    assert not Membership.objects.filter(organization=org_a, user=other).exists()
    assert not Membership.objects.filter(organization=org_b, user=user).exists()


@pytest.mark.django_db
def test_api_create_organization(client, user):
    access = _access_token(client, user.email)
    resp = client.post(
        "/api/v1/orgs/",
        data=json.dumps({"name": "API Fleet"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["name"] == "API Fleet"
    assert body["status"] == OrganizationStatus.ACTIVE
    assert body["my_role"] == MembershipRole.OWNER
    assert body["permissions"]["can_invite"] is True
    assert body["account_id"]

    org_id = body["id"]
    assert Membership.objects.filter(
        organization_id=org_id,
        user=user,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    ).exists()

    # Appears in list for creator.
    listed = client.get(
        "/api/v1/orgs/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert listed.status_code == 200
    assert any(row["id"] == org_id for row in listed.json())


@pytest.mark.django_db
def test_api_create_requires_auth(client):
    resp = client.post(
        "/api/v1/orgs/",
        data=json.dumps({"name": "No Auth"}),
        content_type="application/json",
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_api_create_rejects_blank_name(client, user):
    access = _access_token(client, user.email)
    resp = client.post(
        "/api/v1/orgs/",
        data=json.dumps({"name": "   "}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 400
