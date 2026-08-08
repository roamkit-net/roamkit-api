"""Bind a team billing Account to an Organization (ADR 020).

Does not touch eSIM inventory ownership (``Esim.account`` is a later PR).
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.billing.models import Account, AccountKind
from apps.organizations.models import Organization, OrganizationStatus


@transaction.atomic
def create_organization(
    *,
    name: str,
    status: str = OrganizationStatus.ACTIVE,
) -> Organization:
    """Create an Organization with a new empty team Account.

    The creator's personal Account is never converted or merged.
    """
    account = Account.objects.create(
        kind=AccountKind.ORGANIZATION,
        user=None,
        balance=Decimal("0"),
        version=0,
    )
    return Organization.objects.create(
        name=name,
        status=status,
        account=account,
    )
