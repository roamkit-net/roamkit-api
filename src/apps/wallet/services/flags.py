"""ADR 018 cutover feature flags (read-only helpers).

Defaults are off. ``SHADOW_MODE`` gates Phase 1 dual-path compare
(``apps.wallet.services.shadow``). deposit-info / default WalletAddress wiring
is later Cutover PRs.
"""

from __future__ import annotations

from dataclasses import dataclass

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
