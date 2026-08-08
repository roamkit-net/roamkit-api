"""Resolve active Account context (ADR 020).

Team context is selected via ``organization_id`` only. Client-supplied
``account_id`` is never used as an authorization source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from rest_framework.exceptions import NotFound, PermissionDenied

from apps.billing.services import ensure_billing_account
from apps.organizations.models import (
    Membership,
    MembershipStatus,
    Organization,
    OrganizationStatus,
)
from apps.organizations.permissions import OrgPermissions, permissions_for_role

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.billing.models import Account


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Resolved spend/inventory Account after server-side authorization."""

    kind: str  # "personal" | "organization"
    account: Account
    organization: Organization | None
    membership: Membership | None
    role: str | None
    permissions: OrgPermissions | None


def resolve_personal_context(user: User) -> AccountContext:
    """Personal Account for the authenticated user."""
    account = ensure_billing_account(user)
    return AccountContext(
        kind="personal",
        account=account,
        organization=None,
        membership=None,
        role=None,
        permissions=None,
    )


def resolve_organization_context(
    user: User,
    organization_id: UUID | str,
) -> AccountContext:
    """Resolve team Account via ``organization_id`` + active membership.

    * No membership / unknown org → 404 (hide existence).
    * Membership suspended/revoked → 403.
    * Active membership → team ``Organization.account``.
    """
    try:
        org = Organization.objects.select_related("account").get(pk=organization_id)
    except (Organization.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFound(detail="Not found.") from exc

    membership = (
        Membership.objects.filter(organization=org, user=user)
        .select_related("organization", "organization__account")
        .first()
    )
    if membership is None:
        raise NotFound(detail="Not found.")

    if membership.status != MembershipStatus.ACTIVE:
        raise PermissionDenied(detail="Membership is not active.")

    return AccountContext(
        kind="organization",
        account=org.account,
        organization=org,
        membership=membership,
        role=membership.role,
        permissions=permissions_for_role(membership.role),
    )


def require_org_mutation(context: AccountContext) -> None:
    """Block mutations when Organization is suspended or archived."""
    org = context.organization
    if org is None:
        raise PermissionDenied(detail="Organization context required.")
    if org.status != OrganizationStatus.ACTIVE:
        raise PermissionDenied(
            detail="Organization is not active; mutations are disabled."
        )
