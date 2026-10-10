"""Read-only partner dashboard summary (ADR 023, ADR 024).

The caller passes an already resolved ``PartnerChannel``. This service does
not lock rows, open a transaction, create Accounts, or write.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Sum

from apps.billing.models import Account
from apps.billing.partner_channel import PartnerChannel, PartnerMarginAccrual

_ZERO = Decimal("0.000000")


@dataclass(frozen=True, slots=True)
class PartnerAccrualCounts:
    order: int
    topup: int
    subscription: int
    total: int


@dataclass(frozen=True, slots=True)
class PartnerSummary:
    total_earned: Decimal
    available_balance: Decimal
    accrual_counts: PartnerAccrualCounts


class PartnerSummaryService:
    """Aggregate stored accruals and one Account balance cache."""

    def summarize(self, partner_channel: PartnerChannel) -> PartnerSummary:
        """Legacy TEAM summary. The Account is ``organization.account``."""
        return self._calculate(partner_channel, partner_channel.organization.account)

    def summarize_for_account(
        self,
        partner_channel: PartnerChannel,
        account: Account,
    ) -> PartnerSummary:
        """Same totals for an Account the caller already resolved."""
        return self._calculate(partner_channel, account)

    def _calculate(
        self,
        partner_channel: PartnerChannel,
        account: Account,
    ) -> PartnerSummary:
        grouped = (
            PartnerMarginAccrual.objects.filter(partner_channel=partner_channel)
            .values("source_type")
            .annotate(
                row_count=Count("pk"),
                earned=Sum("partner_share"),
            )
        )
        by_type: dict[str, int] = {}
        total_count = 0
        total_earned = _ZERO
        for row in grouped:
            count = int(row["row_count"])
            by_type[row["source_type"]] = count
            total_count += count
            earned = row["earned"]
            if earned is not None:
                total_earned += earned
        account.refresh_from_db(fields=["balance"])
        balance = account.balance
        return PartnerSummary(
            total_earned=total_earned,
            available_balance=balance if balance > 0 else _ZERO,
            accrual_counts=PartnerAccrualCounts(
                order=by_type.get("order", 0),
                topup=by_type.get("topup", 0),
                subscription=by_type.get("subscription", 0),
                total=total_count,
            ),
        )


partner_summary_service = PartnerSummaryService()
