"""Chain Policy + Confirmation Policy (RFC 006 — chain-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings

from apps.wallet.models import DepositObservation, WalletChain
from apps.wallet.services.hd import normalize_evm_address


class ConfirmationOutcome(StrEnum):
    """Result of Confirmation Policy evaluation (not a persisted status alone)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ConfirmationResult:
    outcome: ConfirmationOutcome
    reason: str = ""


class ChainPolicy:
    """Per-chain parameters consumed by Confirmation Policy (RFC 006).

    Concrete numbers live in settings / ADR Chain Policy — not in adapters.
    """

    def min_confirmations(self, chain: str) -> int:
        if chain == WalletChain.POLYGON:
            return int(settings.POLYGON_MIN_CONFIRMATIONS)
        raise ValueError(f"unsupported chain for ChainPolicy: {chain}")

    def accepted_token_contract(self, chain: str) -> str:
        if chain == WalletChain.POLYGON:
            return normalize_evm_address(settings.POLYGON_USDT_CONTRACT)
        raise ValueError(f"unsupported chain for ChainPolicy: {chain}")


class ConfirmationPolicy:
    """Decide Pending Confirmation → Confirmed | Rejected | stay Pending.

    Does not call CreditService. Cap 3 owns Confirmed → conversion.
    """

    def __init__(self, chain_policy: ChainPolicy | None = None) -> None:
        self.chain_policy = chain_policy or ChainPolicy()

    def evaluate(self, observation: DepositObservation) -> ConfirmationResult:
        chain = observation.chain
        accepted = self.chain_policy.accepted_token_contract(chain)
        token = normalize_evm_address(observation.token_contract)
        if token != accepted:
            return ConfirmationResult(
                ConfirmationOutcome.REJECTED,
                reason="unaccepted_asset",
            )

        required = self.chain_policy.min_confirmations(chain)
        if observation.confirmations < required:
            return ConfirmationResult(
                ConfirmationOutcome.PENDING,
                reason=f"insufficient_confirmations:{observation.confirmations}<{required}",
            )

        return ConfirmationResult(ConfirmationOutcome.CONFIRMED, reason="")
