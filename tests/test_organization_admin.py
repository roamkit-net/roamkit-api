"""Django admin creates an organization without converting a personal account."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.billing.models import AccountKind
from apps.billing.services import ensure_billing_account
from apps.organizations.models import MembershipRole, MembershipStatus, Organization
from apps.organizations.services.account_binding import create_organization

User = get_user_model()


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="SecurePass1!",
    )


def _inline(prefix: str = "memberships", **rows: str) -> dict[str, str]:
    payload = {
        f"{prefix}-TOTAL_FORMS": "0",
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    payload.update(rows)
    return payload


def _staff() -> User:
    staff = _user("staff")
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    return staff


@pytest.mark.django_db
def test_admin_add_creates_team_account_and_owner_membership() -> None:
    owner = _user("owner")
    personal = ensure_billing_account(owner)
    client = Client()
    client.force_login(_staff())

    response = client.post(
        "/admin/organizations/organization/add/",
        {
            "name": "North fleet",
            "status": "active",
            "owner": str(owner.pk),
            **_inline(),
        },
    )

    assert response.status_code == 302
    org = Organization.objects.get(name="North fleet")
    assert org.account_id != personal.pk
    assert org.account.kind == AccountKind.ORGANIZATION
    assert org.account.user_id is None
    assert org.account.balance == 0
    membership = org.memberships.get()
    assert membership.user_id == owner.pk
    assert membership.role == MembershipRole.OWNER
    assert membership.status == MembershipStatus.ACTIVE
    personal.refresh_from_db()
    assert personal.kind == AccountKind.PERSONAL
    assert personal.user_id == owner.pk


@pytest.mark.django_db
def test_admin_add_without_owner_creates_nothing() -> None:
    client = Client()
    client.force_login(_staff())

    response = client.post(
        "/admin/organizations/organization/add/",
        {"name": "No owner", "status": "active", **_inline()},
    )

    assert response.status_code == 200
    assert Organization.objects.filter(name="No owner").count() == 0


@pytest.mark.django_db
def test_admin_change_edits_name_and_keeps_the_team_account() -> None:
    owner = _user("owner")
    org = create_organization(name="Old name", actor=owner)
    account_id = org.account_id
    membership = org.memberships.get()
    client = Client()
    client.force_login(_staff())

    response = client.post(
        f"/admin/organizations/organization/{org.pk}/change/",
        {
            "name": "New name",
            "status": "suspended",
            **_inline(
                **{
                    "memberships-TOTAL_FORMS": "1",
                    "memberships-INITIAL_FORMS": "1",
                    "memberships-0-id": str(membership.pk),
                    "memberships-0-user": str(membership.user_id),
                    "memberships-0-role": membership.role,
                    "memberships-0-status": membership.status,
                }
            ),
        },
    )

    assert response.status_code == 302
    org.refresh_from_db()
    assert org.name == "New name"
    assert org.status == "suspended"
    assert org.account_id == account_id
    assert org.account.kind == AccountKind.ORGANIZATION
    assert org.account.user_id is None
