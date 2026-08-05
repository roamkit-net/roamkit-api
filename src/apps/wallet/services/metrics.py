"""Wallet Platform ops metrics (ADR 017 Cap — Wallet Metrics).

Counters for allocation / observation / confirmation / convert.
Not a Credits source of truth — ledger remains authoritative via CreditService.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Count, Max, Sum

from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    WalletAddress,
    WalletAddressStatus,
    WalletIdentity,
)

_MONEY_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class WalletMetrics:
    """Operational metrics — never treat as ledger authority."""

    wallet_identity_count: int
    wallet_address_active: int
    wallet_address_retired: int
    derivation_index_max: int | None
    observation_counts: dict[str, int]
    pending_confirmation: int
    confirmed_awaiting_convert: int
    conversion_started: int
    credited_count: int
    credited_amount_total: Decimal
    rejected_count: int
    expired_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_identity_count": self.wallet_identity_count,
            "wallet_address_active": self.wallet_address_active,
            "wallet_address_retired": self.wallet_address_retired,
            "derivation_index_max": self.derivation_index_max,
            "observation_counts": dict(self.observation_counts),
            "pending_confirmation": self.pending_confirmation,
            "confirmed_awaiting_convert": self.confirmed_awaiting_convert,
            "conversion_started": self.conversion_started,
            "credited_count": self.credited_count,
            "credited_amount_total": f"{self.credited_amount_total:.6f}",
            "rejected_count": self.rejected_count,
            "expired_count": self.expired_count,
        }


def collect_wallet_metrics() -> WalletMetrics:
    """Aggregate Wallet Platform counters from domain tables."""
    identity_count = WalletIdentity.objects.count()
    addr_rows = (
        WalletAddress.objects.values("status")
        .annotate(n=Count("id"))
        .order_by("status")
    )
    addr_counts = {row["status"]: int(row["n"]) for row in addr_rows}
    active = addr_counts.get(WalletAddressStatus.ACTIVE, 0)
    retired = addr_counts.get(WalletAddressStatus.RETIRED, 0)
    index_max = WalletAddress.objects.aggregate(m=Max("derivation_index"))["m"]

    obs_rows = (
        DepositObservation.objects.values("status")
        .annotate(n=Count("id"))
        .order_by("status")
    )
    obs_counts = {row["status"]: int(row["n"]) for row in obs_rows}
    for status in ObservationStatus.values:
        obs_counts.setdefault(status, 0)

    credited_agg = DepositObservation.objects.filter(
        status=ObservationStatus.CREDITED
    ).aggregate(n=Count("id"), total=Sum("amount"))
    credited_total = Decimal(credited_agg["total"] or 0).quantize(_MONEY_QUANT)

    return WalletMetrics(
        wallet_identity_count=identity_count,
        wallet_address_active=active,
        wallet_address_retired=retired,
        derivation_index_max=None if index_max is None else int(index_max),
        observation_counts=obs_counts,
        pending_confirmation=obs_counts[ObservationStatus.PENDING_CONFIRMATION],
        confirmed_awaiting_convert=obs_counts[ObservationStatus.CONFIRMED],
        conversion_started=obs_counts[ObservationStatus.CONVERSION_STARTED],
        credited_count=int(credited_agg["n"] or 0),
        credited_amount_total=credited_total,
        rejected_count=obs_counts[ObservationStatus.REJECTED],
        expired_count=obs_counts[ObservationStatus.EXPIRED],
    )
