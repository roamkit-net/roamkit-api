"""Wallet ops status / recovery helpers (ADR 017 Cap — Wallet Operations)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import Count

from apps.wallet.models import DepositObservation, ObservationStatus, WalletAddress
from apps.wallet.services.conversion import CreditConversionService


@dataclass(frozen=True)
class WalletOpsStatus:
    """Snapshot for ops drills — not a Credits SoT."""

    observation_counts: dict[str, int]
    confirmed_awaiting_convert: int
    conversion_started: int
    pending_confirmation: int
    wallet_address_count: int
    seed_configured: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_counts": dict(self.observation_counts),
            "confirmed_awaiting_convert": self.confirmed_awaiting_convert,
            "conversion_started": self.conversion_started,
            "pending_confirmation": self.pending_confirmation,
            "wallet_address_count": self.wallet_address_count,
            "seed_configured": self.seed_configured,
        }


def collect_wallet_ops_status() -> WalletOpsStatus:
    rows = (
        DepositObservation.objects.values("status")
        .annotate(n=Count("id"))
        .order_by("status")
    )
    counts = {row["status"]: int(row["n"]) for row in rows}
    for status in ObservationStatus.values:
        counts.setdefault(status, 0)

    return WalletOpsStatus(
        observation_counts=counts,
        confirmed_awaiting_convert=counts.get(ObservationStatus.CONFIRMED, 0),
        conversion_started=counts.get(ObservationStatus.CONVERSION_STARTED, 0),
        pending_confirmation=counts.get(ObservationStatus.PENDING_CONFIRMATION, 0),
        wallet_address_count=WalletAddress.objects.count(),
        seed_configured=bool(
            (getattr(settings, "WALLET_HD_MNEMONIC", "") or "").strip()
        ),
    )


def iter_convertible_observations(*, limit: int | None = None):
    """Confirmed or Conversion Started — eligible for CreditConversionService."""
    qs = DepositObservation.objects.filter(
        status__in=(
            ObservationStatus.CONFIRMED,
            ObservationStatus.CONVERSION_STARTED,
        )
    ).order_by("confirmed_at", "observed_at")
    if limit is not None:
        qs = qs[:limit]
    return qs


def resume_converts(*, apply: bool, limit: int | None = None) -> dict[str, Any]:
    """Resume Confirmed / Conversion Started → Credited.

    Dry-run (``apply=False``) lists candidates only. Never invents a side ledger.
    """
    svc = CreditConversionService()
    candidates = list(iter_convertible_observations(limit=limit))
    results: list[dict[str, str]] = []
    credited = 0
    errors = 0

    for obs in candidates:
        item = {
            "id": str(obs.pk),
            "status": obs.status,
            "identity": f"{obs.chain}:{obs.tx_hash}:{obs.log_index}",
        }
        if not apply:
            item["action"] = "would_convert"
            results.append(item)
            continue
        try:
            entry = svc.convert(obs)
            item["action"] = "credited"
            item["ledger_entry_id"] = str(entry.pk)
            credited += 1
        except Exception as exc:  # noqa: BLE001 — ops drill must report all failures
            item["action"] = "error"
            item["error"] = str(exc)
            errors += 1
        results.append(item)

    return {
        "apply": apply,
        "candidates": len(candidates),
        "credited": credited,
        "errors": errors,
        "results": results,
    }
