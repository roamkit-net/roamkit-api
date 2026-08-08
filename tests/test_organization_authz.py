"""Organization authz + read API (ADR 020 / PR3)."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.exceptions import PermissionDenied

from apps.organizations.exceptions import LastOwnerError, NotAllowedError
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.permissions import permissions_for_role
from apps.organizations.services import (
    create_organization,
    require_manage_members,
    require_org_mutation,
    require_spend,
    resolve_organization_context,
    resolve_personal_context,
    revoke_membership,
    set_member_role,
    set_organization_status,
    transfer_ownership,
)

User = get_user_model()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password="SecurePass1!")


@pytest.fixture
def member_user(db):
    return User.objects.create_user(email="member@example.com", password="SecurePass1!")


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(email="viewer@example.com", password="SecurePass1!")


@pytest.fixture
def stranger(db):
    return User.objects.create_user(
        email="stranger@example.com", password="SecurePass1!"
    )


def _access_token(client: Client, email: str, password: str = "SecurePass1!") -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": password}),
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
def org_a(owner):
    return create_organization(name="Org A", actor=owner)


@pytest.fixture
def org_b(stranger):
    return create_organization(name="Org B", actor=stranger)


# --- permissions matrix ---


def test_viewer_cannot_spend_or_manage():
    perms = permissions_for_role(MembershipRole.VIEWER)
    assert perms.can_view is True
    assert perms.can_spend is False
    assert perms.can_manage_members is False
    assert perms.can_invite is False


def test_member_can_spend_not_manage():
    perms = permissions_for_role(MembershipRole.MEMBER)
    assert perms.can_spend is True
    assert perms.can_manage_members is False
    assert perms.can_transfer_ownership is False


# --- context resolve ---


@pytest.mark.django_db
def test_personal_context_uses_personal_account(owner):
    ctx = resolve_personal_context(owner)
    assert ctx.kind == "personal"
    assert ctx.account.user_id == owner.pk
    assert ctx.organization is None


@pytest.mark.django_db
def test_org_context_resolves_team_account_not_personal(owner, org_a):
    personal = resolve_personal_context(owner).account
    ctx = resolve_organization_context(owner, org_a.pk)
    assert ctx.kind == "organization"
    assert ctx.account.pk == org_a.account_id
    assert ctx.account.pk != personal.pk
    assert ctx.role == MembershipRole.OWNER


@pytest.mark.django_db
def test_foreign_organization_id_is_404(owner, org_b):
    from rest_framework.exceptions import NotFound

    with pytest.raises(NotFound):
        resolve_organization_context(owner, org_b.pk)


@pytest.mark.django_db
def test_unknown_organization_id_is_404(owner):
    from rest_framework.exceptions import NotFound

    with pytest.raises(NotFound):
        resolve_organization_context(owner, uuid.uuid4())


@pytest.mark.django_db
def test_revoked_membership_is_403(owner, org_a, member_user):
    m = _add_member(org_a, member_user, MembershipRole.MEMBER)
    m.status = MembershipStatus.REVOKED
    m.save(update_fields=["status", "updated_at"])
    with pytest.raises(PermissionDenied):
        resolve_organization_context(member_user, org_a.pk)


@pytest.mark.django_db
def test_suspended_membership_is_403(owner, org_a, member_user):
    m = _add_member(org_a, member_user, MembershipRole.MEMBER)
    m.status = MembershipStatus.SUSPENDED
    m.save(update_fields=["status", "updated_at"])
    with pytest.raises(PermissionDenied):
        resolve_organization_context(member_user, org_a.pk)


@pytest.mark.django_db
def test_archived_org_blocks_mutations(owner, org_a):
    org_a.status = OrganizationStatus.ARCHIVED
    org_a.save(update_fields=["status", "updated_at"])
    ctx = resolve_organization_context(owner, org_a.pk)
    with pytest.raises(PermissionDenied):
        require_org_mutation(ctx)
    with pytest.raises(PermissionDenied):
        require_spend(ctx)


@pytest.mark.django_db
def test_viewer_spend_denied(owner, org_a, viewer_user):
    _add_member(org_a, viewer_user, MembershipRole.VIEWER)
    ctx = resolve_organization_context(viewer_user, org_a.pk)
    with pytest.raises(PermissionDenied):
        require_spend(ctx)
    with pytest.raises(PermissionDenied):
        require_manage_members(ctx)


# --- membership invariants ---


@pytest.mark.django_db
def test_transfer_ownership(owner, org_a, member_user):
    _add_member(org_a, member_user, MembershipRole.ADMIN)
    transfer_ownership(actor=owner, organization_id=org_a.pk, new_owner=member_user)
    assert (
        Membership.objects.get(organization=org_a, user=member_user).role
        == MembershipRole.OWNER
    )
    assert (
        Membership.objects.get(organization=org_a, user=owner).role
        == MembershipRole.ADMIN
    )


@pytest.mark.django_db
def test_cannot_revoke_sole_owner(owner, org_a):
    with pytest.raises(LastOwnerError):
        revoke_membership(actor=owner, organization_id=org_a.pk, target_user=owner)


@pytest.mark.django_db
def test_member_cannot_set_roles(owner, org_a, member_user, viewer_user):
    _add_member(org_a, member_user, MembershipRole.MEMBER)
    _add_member(org_a, viewer_user, MembershipRole.VIEWER)
    with pytest.raises(PermissionDenied):
        set_member_role(
            actor=member_user,
            organization_id=org_a.pk,
            target_user=viewer_user,
            role=MembershipRole.MEMBER,
        )


@pytest.mark.django_db
def test_set_organization_status_owner_only(owner, org_a, member_user):
    _add_member(org_a, member_user, MembershipRole.ADMIN)
    with pytest.raises(PermissionDenied):
        set_organization_status(
            actor=member_user,
            organization_id=org_a.pk,
            status=OrganizationStatus.SUSPENDED,
        )
    set_organization_status(
        actor=owner,
        organization_id=org_a.pk,
        status=OrganizationStatus.SUSPENDED,
    )
    org_a.refresh_from_db()
    assert org_a.status == OrganizationStatus.SUSPENDED
    with pytest.raises(NotAllowedError):
        # admin cannot reactivate
        set_organization_status(
            actor=member_user,
            organization_id=org_a.pk,
            status=OrganizationStatus.ACTIVE,
        )


# --- HTTP read API ---


@pytest.mark.django_db
def test_list_orgs_only_mine(client, owner, org_a, org_b):
    access = _access_token(client, owner.email)
    resp = client.get("/api/v1/orgs/", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert str(org_a.pk) in ids
    assert str(org_b.pk) not in ids


@pytest.mark.django_db
def test_detail_cross_org_404(client, owner, org_b):
    access = _access_token(client, owner.email)
    resp = client.get(
        f"/api/v1/orgs/{org_b.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_ok_with_permissions(client, owner, org_a):
    access = _access_token(client, owner.email)
    resp = client.get(
        f"/api/v1/orgs/{org_a.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["my_role"] == MembershipRole.OWNER
    assert body["permissions"]["can_spend"] is True
    assert body["account_id"] == str(org_a.account_id)


@pytest.mark.django_db
def test_members_list_viewer_ok(client, owner, org_a, viewer_user):
    _add_member(org_a, viewer_user, MembershipRole.VIEWER)
    access = _access_token(client, viewer_user.email)
    resp = client.get(
        f"/api/v1/orgs/{org_a.pk}/members/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 200
    emails = {row["user_email"] for row in resp.json()}
    assert owner.email in emails
    assert viewer_user.email in emails


@pytest.mark.django_db
def test_members_list_foreign_404(client, owner, org_b):
    access = _access_token(client, owner.email)
    resp = client.get(
        f"/api/v1/orgs/{org_b.pk}/members/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_revoked_member_detail_403(client, owner, org_a, member_user):
    m = _add_member(org_a, member_user, MembershipRole.MEMBER)
    m.status = MembershipStatus.REVOKED
    m.save(update_fields=["status", "updated_at"])
    access = _access_token(client, member_user.email)
    resp = client.get(
        f"/api/v1/orgs/{org_a.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_no_account_id_query_authz_bypass(client, owner, org_a, org_b):
    """Passing account_id must not grant access to another org's Account."""
    access = _access_token(client, owner.email)
    resp = client.get(
        f"/api/v1/orgs/{org_b.pk}/",
        {"account_id": str(org_a.account_id)},
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_feature_flag_off_404(client, owner, org_a, settings):
    settings.ORGANIZATIONS_ENABLED = False
    access = _access_token(client, owner.email)
    resp = client.get("/api/v1/orgs/", HTTP_AUTHORIZATION=f"Bearer {access}")
    assert resp.status_code == 404
