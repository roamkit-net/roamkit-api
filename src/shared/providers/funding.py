"""Funding Provider protocol and DTOs (ADR 017 / RFC 005)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


class FundingProviderError(Exception):
    """Base error for Funding Provider adapters."""


class FundingProviderUnsupportedError(FundingProviderError):
    """Chain/asset/capability not supported by this adapter."""


@dataclass(frozen=True)
class FundingProviderMetadata:
    """Catalog metadata for UX — never Credits authority."""

    provider_id: str
    display_name: str
    assets: tuple[str, ...]
    chains: tuple[str, ...]
    network_labels: dict[str, str] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ("deposit", "status", "metadata")
    limits_note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FundingDepositRequest:
    """Destination contract inputs for ``deposit()``."""

    wallet_address_id: UUID
    destination_address: str
    chain: str
    asset: str


@dataclass(frozen=True)
class FundingDepositGuide:
    """Provider guide to deliver Asset to a RoamKit WalletAddress.

    Does not imply Credits. On-chain confirmation is Observation (RFC 006).
    """

    provider_id: str
    funding_source: str
    session_id: str
    wallet_address_id: UUID
    destination_address: str
    chain: str
    asset: str
    network_label: str
    instructions: str
    provider_url: str | None = None


@dataclass(frozen=True)
class FundingStatus:
    """Provider-side progress for UX/ops — never Credits SoT."""

    provider_id: str
    session_id: str
    state: str
    message: str
    destination_address: str | None = None
    chain: str | None = None
    asset: str | None = None


class FundingProvider(Protocol):
    """Pluggable Funding Provider adapter (RFC 005 contract)."""

    def metadata(self) -> FundingProviderMetadata:
        """Return provider id, Asset/Chain labels, limits."""

    def deposit(self, request: FundingDepositRequest) -> FundingDepositGuide:
        """Guide or move Asset to the given RoamKit WalletAddress + Chain."""

    def status(self, session_id: str) -> FundingStatus:
        """Return provider-side status for UX/ops (never ledger authority)."""
