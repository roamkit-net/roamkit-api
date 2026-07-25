"""Subscription renewal — debit prepaid credits on Celery beat (ADR-010)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.exceptions import (
    BillingDisabledError,
    InsufficientFundsError,
    SubscriptionsDisabledError,
)
from apps.billing.models import LedgerReferenceType, Subscription
from apps.billing.services.credit import CreditService, credit_service
from shared.events.billing_events import (
    CreditDebited,
    SubscriptionPaused,
    SubscriptionRenewed,
)
from shared.events.event_bus import event_bus

if TYPE_CHECKING:
    from apps.billing.models import CreditLedgerEntry

logger = logging.getLogger(__name__)

# Schema has no period_days; v1 advances one calendar month-ish (30 days).
SUBSCRIPTION_PERIOD_DAYS = 30


class SubscriptionService:
    """Renew due subscriptions against prepaid credits.

    Flow per subscription (``select_for_update``):
    - funded → ``CreditService.debit(SUBSCRIPTION)`` + bump ``next_billing_date``
    - underfunded → ``PAUSED`` + ``SubscriptionPaused`` (email → /me/deposit)

    Gated by ``BILLING_ENABLED`` and ``SUBSCRIPTIONS_ENABLED``.
    """

    def __init__(self, *, credits: CreditService | None = None) -> None:
        self._credits = credits or credit_service

    def renew_due(self, *, as_of: date | None = None) -> dict[str, int]:
        """Process all ``ACTIVE`` subscriptions due on or before ``as_of``."""
        self._require_enabled()
        today = as_of or timezone.localdate()
        due_ids = list(
            Subscription.objects.filter(
                status=Subscription.Status.ACTIVE,
                next_billing_date__lte=today,
            )
            .order_by("next_billing_date", "pk")
            .values_list("pk", flat=True)
        )
        stats = {"renewed": 0, "paused": 0, "skipped": 0}
        for sub_id in due_ids:
            result = self.renew_one(sub_id, as_of=today)
            stats[result] = stats.get(result, 0) + 1
        return stats

    def renew_one(
        self,
        subscription_id,
        *,
        as_of: date | None = None,
    ) -> str:
        """Renew a single subscription. Returns ``renewed``|``paused``|``skipped``."""
        self._require_enabled()
        today = as_of or timezone.localdate()

        events: list[CreditDebited | SubscriptionRenewed | SubscriptionPaused] = []
        outcome = "skipped"
        with transaction.atomic():
            try:
                locked = (
                    Subscription.objects.select_for_update()
                    .select_related("account", "esim")
                    .get(pk=subscription_id)
                )
            except Subscription.DoesNotExist:
                return "skipped"

            if locked.status != Subscription.Status.ACTIVE:
                return "skipped"
            if locked.next_billing_date > today:
                return "skipped"

            billing_date = locked.next_billing_date
            idempotency_key = (
                f"subscription-renew:{locked.pk}:{billing_date.isoformat()}"
            )

            try:
                entry = self._credits.debit(
                    locked.account,
                    locked.price_per_period,
                    reference_type=LedgerReferenceType.SUBSCRIPTION,
                    reference_id=str(locked.pk),
                    idempotency_key=idempotency_key,
                )
            except InsufficientFundsError as exc:
                locked.status = Subscription.Status.PAUSED
                locked.save(update_fields=["status", "updated_at"])
                events.append(
                    SubscriptionPaused(
                        subscription_id=str(locked.pk),
                        account_id=str(locked.account_id),
                        esim_id=str(locked.esim_id),
                        amount_required=locked.price_per_period,
                        balance=(
                            exc.account_balance
                            if exc.account_balance is not None
                            else locked.account.balance
                        ),
                        deposit_url=self._deposit_url(),
                        next_billing_date=locked.next_billing_date,
                        created_at=timezone.now(),
                    )
                )
                outcome = "paused"
            else:
                # Idempotent replay of the same billing date: still advance only
                # when the ledger entry is new for this billing_date, or when
                # next_billing_date still equals that date.
                if locked.next_billing_date == billing_date:
                    locked.next_billing_date = billing_date + timedelta(
                        days=SUBSCRIPTION_PERIOD_DAYS
                    )
                    locked.save(update_fields=["next_billing_date", "updated_at"])

                events.extend(
                    self._renewed_events(locked, entry, amount=locked.price_per_period)
                )
                outcome = "renewed"

        for event in events:
            event_bus.publish(event)
        if outcome == "paused":
            logger.info(
                "Subscription %s paused (insufficient funds)",
                subscription_id,
            )
        return outcome

    def _renewed_events(
        self,
        subscription: Subscription,
        entry: CreditLedgerEntry,
        *,
        amount,
    ) -> list[CreditDebited | SubscriptionRenewed]:
        return [
            CreditDebited(
                account_id=str(subscription.account_id),
                amount=amount,
                balance_after=entry.balance_after,
                reference_type=LedgerReferenceType.SUBSCRIPTION,
                reference_id=str(subscription.pk),
                ledger_entry_id=str(entry.pk),
                created_at=entry.created_at,
            ),
            SubscriptionRenewed(
                subscription_id=str(subscription.pk),
                account_id=str(subscription.account_id),
                esim_id=str(subscription.esim_id),
                amount=amount,
                balance_after=entry.balance_after,
                next_billing_date=subscription.next_billing_date,
                ledger_entry_id=str(entry.pk),
                created_at=entry.created_at,
            ),
        ]

    @staticmethod
    def _deposit_url() -> str:
        base = str(settings.FRONTEND_BASE_URL).rstrip("/")
        return f"{base}/me/deposit"

    @staticmethod
    def _require_enabled() -> None:
        if not settings.BILLING_ENABLED:
            raise BillingDisabledError("Billing is disabled")
        if not settings.SUBSCRIPTIONS_ENABLED:
            raise SubscriptionsDisabledError("Subscriptions are disabled")


subscription_service = SubscriptionService()
