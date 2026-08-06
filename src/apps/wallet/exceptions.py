"""Wallet domain errors (ADR 017 / RFC 004 / RFC 006)."""

from __future__ import annotations


class WalletError(Exception):
    """Base error for apps.wallet."""


class WalletSeedNotConfiguredError(WalletError):
    """Platform HD mnemonic is missing or empty."""


class WalletAllocationError(WalletError):
    """Address allocation failed after retries (concurrency / integrity)."""


class WalletAddressNotFoundError(WalletError):
    """Expected active address is missing (e.g. rotate without prior allocate)."""


class ObservationError(WalletError):
    """Base error for deposit observation (RFC 006)."""


class ObservationAttributionError(ObservationError):
    """Signal ``to_address`` is not a known RoamKit WalletAddress."""


class ObservationTransitionError(ObservationError):
    """Illegal state transition for a DepositObservation."""
