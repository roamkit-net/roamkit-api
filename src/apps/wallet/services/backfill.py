"""WalletAddress backfill + Data Migration Validation (ADR 018 Phase 0).

Policy (locked for Wallet Product v1 cutover):
**batch pre-allocate** — every billing ``Account`` gets a ``WalletIdentity`` and
an active Polygon ``WalletAddress`` before Phase 1 Shadow. Lazy allocate is
not used for the cutover cohort.

Does not change ``deposit-info`` or Credits. Safe to run with cutover flags off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Count, Q, QuerySet

from apps.billing.models import Account
from apps.wallet.models import (
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)
from apps.wallet.services.allocation import WalletAllocationService
from apps.wallet.services.hd import derive_evm_address, normalize_evm_address


@dataclass(frozen=True)
class MigrationValidationReport:
    """ADR 018 Data Migration Validation checks."""

    accounts_total: int
    accounts_missing_identity: int
    accounts_missing_active_address: int
    orphan_identities: int
    duplicate_active_addresses: int
    missing_index_registry_fields: int
    sample_checked: int
    sample_mismatches: int
    sample_errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.accounts_missing_identity == 0
            and self.accounts_missing_active_address == 0
            and self.orphan_identities == 0
            and self.duplicate_active_addresses == 0
            and self.missing_index_registry_fields == 0
            and self.sample_mismatches == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "accounts_total": self.accounts_total,
            "accounts_missing_identity": self.accounts_missing_identity,
            "accounts_missing_active_address": self.accounts_missing_active_address,
            "orphan_identities": self.orphan_identities,
            "duplicate_active_addresses": self.duplicate_active_addresses,
            "missing_index_registry_fields": self.missing_index_registry_fields,
            "sample_checked": self.sample_checked,
            "sample_mismatches": self.sample_mismatches,
            "sample_errors": list(self.sample_errors),
        }


@dataclass(frozen=True)
class BackfillReport:
    """Dry-run or apply result for batch WalletAddress pre-allocation."""

    mode: str
    chain: str
    accounts_scanned: int
    already_ready: int
    would_allocate: int
    allocated: int
    errors: int
    error_details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "chain": self.chain,
            "accounts_scanned": self.accounts_scanned,
            "already_ready": self.already_ready,
            "would_allocate": self.would_allocate,
            "allocated": self.allocated,
            "errors": self.errors,
            "error_details": list(self.error_details),
        }


def _account_ids_with_active_address(*, chain: str) -> set[UUID]:
    return set(
        WalletAddress.objects.filter(
            chain=chain,
            status=WalletAddressStatus.ACTIVE,
        ).values_list("wallet_identity__account_id", flat=True)
    )


def accounts_needing_backfill(
    *,
    chain: str = WalletChain.POLYGON,
) -> QuerySet[Account]:
    """Accounts without an active ``WalletAddress`` on ``chain``."""
    ready = _account_ids_with_active_address(chain=chain)
    return Account.objects.exclude(pk__in=ready).order_by("created_at")


def run_wallet_backfill(
    *,
    apply: bool = False,
    limit: int | None = None,
    chain: str = WalletChain.POLYGON,
) -> BackfillReport:
    """Pre-allocate WalletIdentity + active WalletAddress for Accounts that lack one.

    Default is dry-run (``apply=False``). Uses ``WalletAllocationService`` only —
    no Credits mutation.
    """
    accounts_total = Account.objects.count()
    missing_qs = accounts_needing_backfill(chain=chain)
    full_missing_count = missing_qs.count()
    already_ready = accounts_total - full_missing_count

    targets = list(missing_qs[:limit] if limit is not None else missing_qs)

    if not apply:
        return BackfillReport(
            mode="dry-run",
            chain=chain,
            accounts_scanned=accounts_total,
            already_ready=already_ready,
            would_allocate=len(targets),
            allocated=0,
            errors=0,
        )

    svc = WalletAllocationService()
    allocated = 0
    errors = 0
    details: list[str] = []
    for account in targets:
        try:
            svc.ensure_active_address(account, chain=chain)
            allocated += 1
        except Exception as exc:  # noqa: BLE001 — ops report must continue
            errors += 1
            details.append(f"account={account.pk} error={exc}")

    return BackfillReport(
        mode="apply",
        chain=chain,
        accounts_scanned=accounts_total,
        already_ready=already_ready,
        would_allocate=len(targets),
        allocated=allocated,
        errors=errors,
        error_details=details,
    )


def validate_wallet_migration(
    *,
    chain: str = WalletChain.POLYGON,
    sample_size: int = 10,
) -> MigrationValidationReport:
    """Run ADR 018 Data Migration Validation checks.

    Gate fails if any hard check is non-zero or sample re-derivation mismatches.
    """
    accounts_total = Account.objects.count()
    missing_active = accounts_needing_backfill(chain=chain).count()
    missing_identity = Account.objects.filter(wallet_identity__isnull=True).count()

    orphan_identities = (
        WalletIdentity.objects.annotate(n=Count("addresses")).filter(n=0).count()
    )

    duplicate_active = (
        WalletAddress.objects.filter(
            chain=chain,
            status=WalletAddressStatus.ACTIVE,
        )
        .values("wallet_identity_id", "chain")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )

    missing_fields = WalletAddress.objects.filter(
        Q(address="") | Q(derivation_index__isnull=True)
    ).count()

    sample_errors: list[str] = []
    sample_mismatches = 0
    sample_checked = 0
    mnemonic = (getattr(settings, "WALLET_HD_MNEMONIC", "") or "").strip()
    sample_qs = WalletAddress.objects.filter(chain=chain).order_by("derivation_index")[
        : max(0, sample_size)
    ]
    for row in sample_qs:
        sample_checked += 1
        if not mnemonic:
            sample_errors.append(
                f"address={row.pk} error=WALLET_HD_MNEMONIC not configured"
            )
            sample_mismatches += 1
            continue
        try:
            expected = derive_evm_address(
                mnemonic=mnemonic,
                derivation_index=row.derivation_index,
            )
        except Exception as exc:  # noqa: BLE001
            sample_errors.append(f"address={row.pk} error={exc}")
            sample_mismatches += 1
            continue
        if normalize_evm_address(row.address) != normalize_evm_address(expected):
            sample_mismatches += 1
            sample_errors.append(
                f"address={row.pk} index={row.derivation_index} "
                f"stored={row.address} expected={expected}"
            )

    return MigrationValidationReport(
        accounts_total=accounts_total,
        accounts_missing_identity=int(missing_identity),
        accounts_missing_active_address=int(missing_active),
        orphan_identities=int(orphan_identities),
        duplicate_active_addresses=int(duplicate_active),
        missing_index_registry_fields=int(missing_fields),
        sample_checked=sample_checked,
        sample_mismatches=sample_mismatches,
        sample_errors=sample_errors,
    )
