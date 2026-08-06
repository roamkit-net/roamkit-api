"""ADR 018 cutover feature flags + Limited Traffic cohort (default off)."""

from __future__ import annotations

from django.test import override_settings

from apps.wallet.services.flags import (
    cutover_ops_snapshot,
    get_cutover_flags,
    is_in_cutover_cohort,
    should_expose_wallet_address,
)


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
    assert cutover_ops_snapshot()["cutover_rollback_status"] == "legacy_only"


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


@override_settings(
    WALLET_ADDRESS_ENABLED=True,
    WALLET_CUTOVER_COHORT_ACCOUNT_IDS="11111111-1111-1111-1111-111111111111",
)
def test_cohort_membership_is_explicit_allowlist() -> None:
    assert is_in_cutover_cohort("11111111-1111-1111-1111-111111111111") is True
    assert is_in_cutover_cohort("22222222-2222-2222-2222-222222222222") is False
    assert should_expose_wallet_address("11111111-1111-1111-1111-111111111111") is True
    assert should_expose_wallet_address("22222222-2222-2222-2222-222222222222") is False
