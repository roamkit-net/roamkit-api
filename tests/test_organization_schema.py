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
from apps.organizations.services import create_organization

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="org-owner@example.com", password="x")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="org-member@example.com", password="x")


@pytest.fixture
def organization(other_user) -> Organization:
    # Owner is other_user so membership constraint tests can still add ``user``.
    return create_organization(name="Fleet Ops", actor=other_user)


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
        role=MembershipRole.MEMBER,
    )
    with pytest.raises(IntegrityError):
        Membership.objects.create(
            organization=organization,
            user=user,
            role=MembershipRole.ADMIN,
        )


@pytest.mark.django_db
def test_at_most_one_active_owner(organization: Organization, user):
    # ``organization`` fixture already has an active owner (other_user).
    with pytest.raises(IntegrityError):
        Membership.objects.create(
            organization=organization,
            user=user,
            role=MembershipRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )


@pytest.mark.django_db
def test_second_owner_allowed_when_previous_not_active(
    organization: Organization, user, other_user
):
    existing = Membership.objects.get(
        organization=organization,
        user=other_user,
        role=MembershipRole.OWNER,
    )
    existing.status = MembershipStatus.REVOKED
    existing.save(update_fields=["status", "updated_at"])
    second = Membership.objects.create(
        organization=organization,
        user=user,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    assert second.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
def test_membership_hard_delete_blocked(organization: Organization, other_user):
    membership = Membership.objects.get(
        organization=organization,
        user=other_user,
    )
    with pytest.raises(HardDeleteViolation):
        membership.delete()
    with pytest.raises(HardDeleteViolation):
        Membership.objects.filter(pk=membership.pk).delete()
    assert Membership.objects.filter(pk=membership.pk).exists()


@pytest.mark.django_db
def test_membership_revoke_via_status(organization: Organization, other_user):
    membership = Membership.objects.get(
        organization=organization,
        user=other_user,
    )
    membership.status = MembershipStatus.REVOKED
    membership.save(update_fields=["status", "updated_at"])
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED


@pytest.mark.django_db
def test_user_can_belong_to_multiple_organizations(user):
    create_organization(name="Org A", actor=user)
    create_organization(name="Org B", actor=user)
    assert Membership.objects.filter(user=user).count() == 2
