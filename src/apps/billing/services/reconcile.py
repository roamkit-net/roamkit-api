"""Balance reconcile (alert) and rebuild orchestration (ADR-010)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Account
from apps.billing.services.credit import CreditService, credit_service
from shared.events.billing_events import BalanceDriftDetected
from shared.events.event_bus import event_bus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BalanceDrift:
    account_id: str
    cached_balance: Decimal
    ledger_sum: Decimal

    @property
    def drift(self) -> Decimal:
        return self.cached_balance - self.ledger_sum


class ReconcileService:
    """Compare ``Account.balance`` cache to ledger SUM; never auto-correct."""

    def __init__(self, *, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def find_drifts(self) -> list[BalanceDrift]:
        drifts: list[BalanceDrift] = []
        for account in Account.objects.order_by("pk").iterator():
            expected = self._credits.ledger_sum(account)
            if account.balance != expected:
                drifts.append(
                    BalanceDrift(
                        account_id=str(account.pk),
                        cached_balance=account.balance,
                        ledger_sum=expected,
                    )
                )
        return drifts

    def reconcile(self) -> dict[str, int]:
        """Scan all accounts, publish/alert on drift. Does not mutate balances."""
        drifts = self.find_drifts()
        detected_at = timezone.now()
        for drift in drifts:
            logger.error(
                "Balance drift account_id=%s cached=%s ledger_sum=%s drift=%s",
                drift.account_id,
                drift.cached_balance,
                drift.ledger_sum,
                drift.drift,
            )
            event_bus.publish(
                BalanceDriftDetected(
                    account_id=drift.account_id,
                    cached_balance=drift.cached_balance,
                    ledger_sum=drift.ledger_sum,
                    drift=drift.drift,
                    detected_at=detected_at,
                )
            )
        return {"checked": Account.objects.count(), "drifts": len(drifts)}


class RebuildService:
    """Explicit operator rebuild of balance cache from ledger."""

    def __init__(self, *, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def rebuild_all(self) -> dict[str, int]:
        repaired = 0
        checked = 0
        for account in Account.objects.order_by("pk").iterator():
            checked += 1
            before = account.balance
            after = self._credits.rebuild_balance_from_ledger(account)
            if before != after:
                repaired += 1
                logger.warning(
                    "Rebuilt balance account_id=%s from %s to %s",
                    account.pk,
                    before,
                    after,
                )
        return {"checked": checked, "repaired": repaired}

    def rebuild_one(self, account: Account) -> Decimal:
        return self._credits.rebuild_balance_from_ledger(account)


reconcile_service = ReconcileService()
rebuild_service = RebuildService()
