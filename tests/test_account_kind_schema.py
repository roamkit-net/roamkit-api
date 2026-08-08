"""Tests for Account.kind + Organization.account binding (ADR 020 / PR2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.billing.models import Account, AccountKind
from apps.billing.services import ensure_billing_account
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.organizations.services import create_organization

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="kind-owner@example.com", password="x")


@pytest.mark.django_db
def test_personal_account_default_kind(user):
    account = ensure_billing_account(user)
    assert account.kind == AccountKind.PERSONAL
    assert account.user_id == user.pk


@pytest.mark.django_db
def test_create_organization_binds_team_account():
    org = create_organization(name="Fleet Ops")
    org.refresh_from_db()
    assert org.account_id is not None
    assert org.account.kind == AccountKind.ORGANIZATION
    assert org.account.user_id is None
    assert org.account.balance == Decimal("0")
    assert org.account.organization.pk == org.pk


@pytest.mark.django_db
def test_organization_account_one_to_one():
    org_a = create_organization(name="Org A")
    with pytest.raises(IntegrityError):
        Organization.objects.create(
            name="Org B",
            account=org_a.account,
        )


@pytest.mark.django_db
def test_personal_account_requires_user():
    with pytest.raises(IntegrityError):
        Account.objects.create(
            kind=AccountKind.PERSONAL,
            user=None,
            balance=Decimal("0"),
        )


@pytest.mark.django_db
def test_organization_account_forbids_user(user):
    with pytest.raises(IntegrityError):
        Account.objects.create(
            kind=AccountKind.ORGANIZATION,
            user=user,
            balance=Decimal("0"),
        )


@pytest.mark.django_db
def test_create_organization_does_not_touch_personal_account(user):
    personal = ensure_billing_account(user)
    org = create_organization(name="Fleet Ops")
    Membership.objects.create(
        organization=org,
        user=user,
        role=MembershipRole.OWNER,
    )
    personal.refresh_from_db()
    assert personal.kind == AccountKind.PERSONAL
    assert personal.pk != org.account_id
    assert personal.balance == Decimal("0")
