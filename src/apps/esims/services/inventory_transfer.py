"""Explicit Account-to-Account eSIM inventory transfer (ADR 020).

Historical orders, top-ups, and ledger rows stay on the source account.
This module does not call CreditService and does not write admin audit logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.billing.models import Account, AccountKind
from apps.esims.exceptions import (
    EsimTransferDestinationError,
    EsimTransferInProgressError,
)
from apps.esims.models import Esim, EsimAutoTopupPolicy, Topup

User = get_user_model()


@dataclass(frozen=True)
class EsimInventoryTransferResult:
    """What the admin layer needs to message and audit a transfer."""

    old_account_id: UUID
    new_account_id: UUID
    transferred: bool
    auto_topup_disabled: bool


def transfer_esim_inventory(
    *, esim_id, destination_user
) -> EsimInventoryTransferResult:
    """Move inventory ownership to ``destination_user``'s personal account.

    Idempotent when that account already owns the eSIM. Refuses a destination
    that is not a personal account, and refuses the move while a top-up is
    still fulfilling. An existing auto top-up policy is disabled and retargeted
    so it cannot spend the new owner's balance.
    """
    if destination_user is None or getattr(destination_user, "pk", None) is None:
        raise EsimTransferDestinationError("Destination user is required")

    with transaction.atomic():
        esim = Esim.objects.select_for_update().get(pk=esim_id)
        try:
            user = User.objects.get(pk=destination_user.pk)
        except User.DoesNotExist as exc:
            raise EsimTransferDestinationError(
                "Destination user does not exist"
            ) from exc

        account = _require_personal_account(
            account=_load_destination_account(user),
            user=user,
        )
        old_account_id = esim.account_id
        if old_account_id == account.pk:
            return EsimInventoryTransferResult(
                old_account_id=old_account_id,
                new_account_id=account.pk,
                transferred=False,
                auto_topup_disabled=False,
            )

        if Topup.objects.filter(
            esim_id=esim.pk,
            status=Topup.Status.FULFILLING,
        ).exists():
            raise EsimTransferInProgressError(
                "A top-up is still fulfilling for this eSIM"
            )

        policy = (
            EsimAutoTopupPolicy.objects.select_for_update()
            .filter(esim_id=esim.pk)
            .first()
        )
        esim.account = account
        esim.user = user
        esim.assigned_user = user
        esim.save(update_fields=["account", "user", "assigned_user", "updated_at"])

        auto_topup_disabled = False
        if policy is not None:
            policy.account = account
            policy.enabled = False
            policy.status = EsimAutoTopupPolicy.Status.DISABLED
            policy.reason = EsimAutoTopupPolicy.Reason.MANUAL_PAUSE
            policy.version = policy.version + 1
            policy.save(
                update_fields=[
                    "account",
                    "enabled",
                    "status",
                    "reason",
                    "version",
                    "updated_at",
                ]
            )
            auto_topup_disabled = True

        return EsimInventoryTransferResult(
            old_account_id=old_account_id,
            new_account_id=account.pk,
            transferred=True,
            auto_topup_disabled=auto_topup_disabled,
        )


def _load_destination_account(user) -> Account | None:
    """Lock the account row that belongs to ``user``, if one exists."""
    return Account.objects.select_for_update().filter(user_id=user.pk).first()


def _require_personal_account(*, account: Account | None, user) -> Account:
    if (
        account is None
        or account.kind != AccountKind.PERSONAL
        or account.user_id != user.pk
    ):
        raise EsimTransferDestinationError(
            "Destination must be an existing user with a personal account"
        )
    return account
