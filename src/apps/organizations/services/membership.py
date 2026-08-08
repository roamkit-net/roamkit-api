"""Membership mutations with ADR 020 invariants.

HTTP invite/write APIs are out of scope for PR3; these services are the
normative implementation for later endpoints and are covered by unit tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.organizations.exceptions import LastOwnerError, NotAllowedError
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationStatus,
)
from apps.organizations.services.authz import (
    require_archive_org,
    require_manage_members,
    require_transfer_ownership,
)
from apps.organizations.services.context import resolve_organization_context

if TYPE_CHECKING:
    from apps.accounts.models import User


def _active_owner_count(organization: Organization) -> int:
    return Membership.objects.filter(
        organization=organization,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    ).count()


@transaction.atomic
def transfer_ownership(
    *,
    actor: User,
    organization_id,
    new_owner: User,
) -> Membership:
    """Transfer sole active owner role to ``new_owner`` (must be active member)."""
    ctx = resolve_organization_context(actor, organization_id)
    require_transfer_ownership(ctx)
    org = ctx.organization
    assert org is not None
    assert ctx.membership is not None

    target = (
        Membership.objects.select_for_update()
        .filter(organization=org, user=new_owner)
        .first()
    )
    if target is None or target.status != MembershipStatus.ACTIVE:
        raise NotAllowedError("New owner must be an active organization member.")

    actor_membership = (
        Membership.objects.select_for_update().filter(pk=ctx.membership.pk).get()
    )
    if actor_membership.role != MembershipRole.OWNER:
        raise NotAllowedError("Only the owner can transfer ownership.")

    if target.pk == actor_membership.pk:
        return actor_membership

    # Demote current owner first so the partial unique owner constraint holds.
    actor_membership.role = MembershipRole.ADMIN
    actor_membership.save(update_fields=["role", "updated_at"])

    target.role = MembershipRole.OWNER
    target.save(update_fields=["role", "updated_at"])
    return target


@transaction.atomic
def set_member_role(
    *,
    actor: User,
    organization_id,
    target_user: User,
    role: str,
) -> Membership:
    """Change a member's role. Owner role changes go through ``transfer_ownership``."""
    if role == MembershipRole.OWNER:
        raise NotAllowedError("Use transfer_ownership to assign the owner role.")

    ctx = resolve_organization_context(actor, organization_id)
    require_manage_members(ctx)
    org = ctx.organization
    assert org is not None

    target = (
        Membership.objects.select_for_update()
        .filter(organization=org, user=target_user)
        .first()
    )
    if target is None or target.status != MembershipStatus.ACTIVE:
        raise NotAllowedError("Target must be an active organization member.")

    if target.role == MembershipRole.OWNER:
        raise NotAllowedError("Cannot change the owner's role; transfer ownership.")

    if role not in {
        MembershipRole.ADMIN,
        MembershipRole.MEMBER,
        MembershipRole.VIEWER,
    }:
        raise NotAllowedError("Invalid role.")

    target.role = role
    target.save(update_fields=["role", "updated_at"])
    return target


@transaction.atomic
def revoke_membership(
    *,
    actor: User,
    organization_id,
    target_user: User,
) -> Membership:
    """Revoke membership (status=revoked). Cannot revoke the sole active owner."""
    ctx = resolve_organization_context(actor, organization_id)
    require_manage_members(ctx)
    org = ctx.organization
    assert org is not None

    target = (
        Membership.objects.select_for_update()
        .filter(organization=org, user=target_user)
        .first()
    )
    if target is None:
        raise NotAllowedError("Target membership not found.")
    if target.status == MembershipStatus.REVOKED:
        return target

    if target.role == MembershipRole.OWNER and target.status == MembershipStatus.ACTIVE:
        if _active_owner_count(org) <= 1:
            raise LastOwnerError("Cannot revoke the sole active owner.")
        if ctx.role != MembershipRole.OWNER:
            raise NotAllowedError("Only the owner can revoke an owner.")

    target.status = MembershipStatus.REVOKED
    target.save(update_fields=["status", "updated_at"])
    return target


@transaction.atomic
def set_organization_status(
    *,
    actor: User,
    organization_id,
    status: str,
) -> Organization:
    """Set Organization status (owner only).

    Reactivation from suspended/archived is allowed for the owner so the
    lifecycle is reversible without hard delete.
    """
    if status not in {
        OrganizationStatus.ACTIVE,
        OrganizationStatus.SUSPENDED,
        OrganizationStatus.ARCHIVED,
    }:
        raise NotAllowedError("Invalid organization status.")

    ctx = resolve_organization_context(actor, organization_id)
    org = ctx.organization
    assert org is not None

    if org.status == OrganizationStatus.ACTIVE:
        require_archive_org(ctx)
    elif ctx.role != MembershipRole.OWNER:
        raise NotAllowedError("Only the owner can change organization status.")

    org.status = status
    org.save(update_fields=["status", "updated_at"])
    return org
