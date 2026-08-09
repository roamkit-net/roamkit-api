"""Bind a team billing Account to an Organization (ADR 020)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction

from apps.billing.models import Account, AccountKind
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationStatus,
)

if TYPE_CHECKING:
    from apps.accounts.models import User


@transaction.atomic
def create_organization(
    *,
    name: str,
    actor: User,
    status: str = OrganizationStatus.ACTIVE,
) -> Organization:
    """Create Organization + empty team Account + active owner Membership.

    Atomic: any failure rolls back Organization, Account, and Membership.
    The creator's personal Account is never converted or merged.
    """
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("Organization name is required.")

    account = Account.objects.create(
        kind=AccountKind.ORGANIZATION,
        user=None,
        balance=Decimal("0"),
        version=0,
    )
    org = Organization.objects.create(
        name=trimmed,
        status=status,
        account=account,
    )
    Membership.objects.create(
        organization=org,
        user=actor,
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    return org
