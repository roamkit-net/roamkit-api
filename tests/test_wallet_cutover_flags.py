"""ADR 018 cutover feature flags (default off)."""

from __future__ import annotations

from django.test import override_settings

from apps.wallet.services.flags import get_cutover_flags


def test_cutover_flags_default_off() -> None:
    flags = get_cutover_flags()
    assert flags.wallet_address_enabled is False
    assert flags.observation_enabled is False
    assert flags.credit_conversion_v2 is False
    assert flags.shadow_mode is False
    assert flags.as_dict() == {
        "WALLET_ADDRESS_ENABLED": False,
        "OBSERVATION_ENABLED": False,
        "CREDIT_CONVERSION_V2": False,
        "SHADOW_MODE": False,
    }


@override_settings(
    WALLET_ADDRESS_ENABLED=True,
    OBSERVATION_ENABLED=True,
    CREDIT_CONVERSION_V2=True,
    SHADOW_MODE=True,
)
def test_cutover_flags_can_be_enabled() -> None:
    flags = get_cutover_flags()
    assert flags.wallet_address_enabled is True
    assert flags.observation_enabled is True
    assert flags.credit_conversion_v2 is True
    assert flags.shadow_mode is True
