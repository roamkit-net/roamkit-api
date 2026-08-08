"""Tests for Organization + Membership schema (ADR 020 / PR1)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.organizations.models import (
    HardDeleteViolation,
    Membership,
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationStatus,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="org-owner@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="org-member@example.com", password="x")


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Fleet Ops")


@pytest.mark.django_db
def test_organization_defaults(organization: Organization):
    assert organization.status == OrganizationStatus.ACTIVE
    assert organization.name == "Fleet Ops"
    assert organization.pk is not None


@pytest.mark.django_db
def test_organization_status_transitions(organization: Organization):
    organization.status = OrganizationStatus.SUSPENDED
    organization.save(update_fields=["status", "updated_at"])
    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.SUSPENDED

    organization.status = OrganizationStatus.ARCHIVED
    organization.save(update_fields=["status", "updated_at"])
    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.ARCHIVED


@pytest.mark.django_db
def test_organization_hard_delete_blocked(organization: Organization):
    with pytest.raises(HardDeleteViolation):
        organization.delete()
    with pytest.raises(HardDeleteViolation):
        Organization.objects.filter(pk=organization.pk).delete()
    assert Organization.objects.filter(pk=organization.pk).exists()


@pytest.mark.django_db
def test_membership_unique_per_user_org(organization: Organization, user, other_user):
    Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
    )
    Membership.objects.create(
        organization=organization,
        user=other_user,
        role=MembershipRole.MEMBER,
    )
    with pytest.raises(IntegrityError):
        Membership.objects.create(
            organization=organization,
            user=user,
            role=MembershipRole.ADMIN,
        )


@pytest.mark.django_db
def test_at_most_one_active_owner(organization: Organization, user, other_user):
    Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    with pytest.raises(IntegrityError):
        Membership.objects.create(
            organization=organization,
            user=other_user,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )


@pytest.mark.django_db
def test_second_owner_allowed_when_previous_not_active(
    organization: Organization, user, other_user
):
    Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
        status=MembershipStatus.REVOKED,
    )
    second = Membership.objects.create(
        organization=organization,
        user=other_user,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    assert second.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
def test_membership_hard_delete_blocked(organization: Organization, user):
    membership = Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
    )
    with pytest.raises(HardDeleteViolation):
        membership.delete()
    with pytest.raises(HardDeleteViolation):
        Membership.objects.filter(pk=membership.pk).delete()
    assert Membership.objects.filter(pk=membership.pk).exists()


@pytest.mark.django_db
def test_membership_revoke_via_status(organization: Organization, user):
    membership = Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
    )
    membership.status = MembershipStatus.REVOKED
    membership.save(update_fields=["status", "updated_at"])
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED


@pytest.mark.django_db
def test_user_can_belong_to_multiple_organizations(user):
    org_a = Organization.objects.create(name="Org A")
    org_b = Organization.objects.create(name="Org B")
    Membership.objects.create(
        organization=org_a,
        user=user,
        role=MembershipRole.OWNER,
    )
    Membership.objects.create(
        organization=org_b,
        user=user,
        role=MembershipRole.MEMBER,
    )
    assert Membership.objects.filter(user=user).count() == 2
