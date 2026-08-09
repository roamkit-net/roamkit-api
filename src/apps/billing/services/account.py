"""Account provisioning helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from apps.billing.models import Account, AccountKind

if TYPE_CHECKING:
    from apps.accounts.models import User


def ensure_billing_account(user: User) -> Account:
    """Idempotently ensure the user has a personal billing Account (balance 0)."""
    account, _created = Account.objects.get_or_create(
        user=user,
        defaults={
            "kind": AccountKind.PERSONAL,
            "balance": Decimal("0"),
            "version": 0,
        },
    )
    return account
