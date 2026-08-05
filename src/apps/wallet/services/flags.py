"""ADR 018 cutover feature flags + Limited Traffic cohort (Phase 2).

Defaults are off. Empty cohort allowlist = instant rollback to legacy ADR 010
for everyone (no deploy / migration required).

Cohort is an explicit Account UUID allowlist — not a percentage rollout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings


@dataclass(frozen=True)
class WalletCutoverFlags:
    """Snapshot of ADR 018 activation flags."""

    wallet_address_enabled: bool
    observation_enabled: bool
    credit_conversion_v2: bool
    shadow_mode: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "WALLET_ADDRESS_ENABLED": self.wallet_address_enabled,
            "OBSERVATION_ENABLED": self.observation_enabled,
            "CREDIT_CONVERSION_V2": self.credit_conversion_v2,
            "SHADOW_MODE": self.shadow_mode,
        }


def get_cutover_flags() -> WalletCutoverFlags:
    """Return current cutover flags from Django settings."""
    return WalletCutoverFlags(
        wallet_address_enabled=bool(getattr(settings, "WALLET_ADDRESS_ENABLED", False)),
        observation_enabled=bool(getattr(settings, "OBSERVATION_ENABLED", False)),
        credit_conversion_v2=bool(getattr(settings, "CREDIT_CONVERSION_V2", False)),
        shadow_mode=bool(getattr(settings, "SHADOW_MODE", False)),
    )


def parse_cutover_cohort_account_ids(raw: str | None = None) -> frozenset[UUID]:
    """Parse comma-separated Account UUIDs. Invalid tokens are skipped."""
    if raw is None:
        raw = getattr(settings, "WALLET_CUTOVER_COHORT_ACCOUNT_IDS", "") or ""
    out: set[UUID] = set()
    for part in str(raw).split(","):
        text = part.strip()
        if not text:
            continue
        try:
            out.add(UUID(text))
        except ValueError:
            continue
    return frozenset(out)


def cutover_cohort_account_ids() -> frozenset[UUID]:
    """Current Limited Traffic allowlist (empty ⇒ legacy-only rollback)."""
    return parse_cutover_cohort_account_ids()


def is_in_cutover_cohort(account_id: UUID | str) -> bool:
    """True when ``account_id`` is on the explicit activation allowlist."""
    try:
        uid = account_id if isinstance(account_id, UUID) else UUID(str(account_id))
    except ValueError:
        return False
    return uid in cutover_cohort_account_ids()


def should_expose_wallet_address(account_id: UUID | str) -> bool:
    """deposit-info returns WalletAddress only when flag on AND in cohort."""
    flags = get_cutover_flags()
    return flags.wallet_address_enabled and is_in_cutover_cohort(account_id)


def should_use_credit_conversion_v2(account_id: UUID | str) -> bool:
    """Credit Conversion V2 only for cohort members when flag on."""
    flags = get_cutover_flags()
    return flags.credit_conversion_v2 and is_in_cutover_cohort(account_id)


def should_use_wallet_money_path(account_id: UUID | str) -> bool:
    """Limited Traffic intake: WalletAddress → Observation → Conversion.

    Requires address + observation + conversion flags and cohort membership.
    Empty cohort or any flag off ⇒ ADR 010 shared-wallet path.
    """
    flags = get_cutover_flags()
    return (
        flags.wallet_address_enabled
        and flags.observation_enabled
        and flags.credit_conversion_v2
        and is_in_cutover_cohort(account_id)
    )


def cutover_ops_snapshot() -> dict[str, Any]:
    """Cohort / rollback fields for wallet_metrics dashboard."""
    flags = get_cutover_flags()
    cohort = cutover_cohort_account_ids()
    limited = (
        bool(cohort)
        and flags.wallet_address_enabled
        and flags.observation_enabled
        and flags.credit_conversion_v2
    )
    if not cohort or not (
        flags.wallet_address_enabled
        or flags.credit_conversion_v2
        or flags.observation_enabled
    ):
        rollback = "legacy_only"
    elif limited:
        rollback = "limited_traffic"
    elif flags.shadow_mode:
        rollback = "shadow"
    else:
        rollback = "flags_partial"
    return {
        "cutover_cohort_size": len(cohort),
        "cutover_rollback_status": rollback,
        "cutover_limited_traffic_active": limited,
    }
