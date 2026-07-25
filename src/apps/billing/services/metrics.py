"""Ops metrics for prepaid credits (ADR-010)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Avg, Count, Sum

from apps.billing.models import CreditLedgerEntry, DepositRequest, LedgerReferenceType
from apps.billing.services.credit import MONEY_QUANT


@dataclass(frozen=True)
class BillingMetrics:
    total_deposited_usdt: Decimal
    deposit_count: int
    average_deposit: Decimal | None
    total_spent_credits: Decimal
    failed_verify_count: int

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "total_deposited_usdt": f"{self.total_deposited_usdt:.6f}",
            "deposit_count": self.deposit_count,
            "average_deposit": (
                f"{self.average_deposit:.6f}"
                if self.average_deposit is not None
                else None
            ),
            "total_spent_credits": f"{self.total_spent_credits:.6f}",
            "failed_verify_count": self.failed_verify_count,
        }


def collect_billing_metrics() -> BillingMetrics:
    """Aggregate deposit / spend / failure stats from the database."""
    completed = DepositRequest.objects.filter(
        status=DepositRequest.Status.COMPLETED
    ).aggregate(
        total=Sum("amount_credited"),
        count=Count("id"),
        avg=Avg("amount_credited"),
    )
    total_deposited = completed["total"] or Decimal("0")
    deposit_count = int(completed["count"] or 0)
    average = completed["avg"]
    if average is not None:
        average = Decimal(average).quantize(MONEY_QUANT)

    spent_agg = CreditLedgerEntry.objects.filter(delta__lt=0).aggregate(
        total=Sum("delta")
    )
    spent_raw = spent_agg["total"] or Decimal("0")
    # Report as positive "spent" magnitude.
    total_spent = (-Decimal(spent_raw)).quantize(MONEY_QUANT)

    failed_verify_count = DepositRequest.objects.filter(
        status=DepositRequest.Status.FAILED
    ).count()

    return BillingMetrics(
        total_deposited_usdt=Decimal(total_deposited).quantize(MONEY_QUANT),
        deposit_count=deposit_count,
        average_deposit=average,
        total_spent_credits=total_spent,
        failed_verify_count=failed_verify_count,
    )


def spend_by_reference_type() -> dict[str, Decimal]:
    """Optional breakdown of spent credits by ledger reference_type."""
    rows = (
        CreditLedgerEntry.objects.filter(delta__lt=0)
        .values("reference_type")
        .annotate(total=Sum("delta"))
    )
    out: dict[str, Decimal] = {}
    for row in rows:
        ref = row["reference_type"]
        total = -Decimal(row["total"] or 0)
        out[ref] = total.quantize(MONEY_QUANT)
    # Ensure known spend types appear even when zero.
    for choice in (
        LedgerReferenceType.ORDER,
        LedgerReferenceType.TOPUP,
        LedgerReferenceType.SUBSCRIPTION,
    ):
        out.setdefault(choice.value, Decimal("0.000000"))
    return out
