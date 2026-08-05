"""Deposit Observation ingest + confirmation advance (RFC 006 Cap 2)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.wallet.exceptions import (
    ObservationAttributionError,
    ObservationTransitionError,
)
from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    WalletAddress,
    WalletChain,
)
from apps.wallet.services.confirmation import (
    ConfirmationOutcome,
    ConfirmationPolicy,
)
from apps.wallet.services.hd import normalize_evm_address

_TERMINAL = frozenset(
    {
        ObservationStatus.CONFIRMED,
        ObservationStatus.CONVERSION_STARTED,
        ObservationStatus.CREDITED,
        ObservationStatus.REJECTED,
        ObservationStatus.EXPIRED,
    }
)


def normalize_tx_hash(value: str) -> str:
    text = (value or "").strip().lower()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66:
        raise ValueError(f"invalid tx_hash: {value!r}")
    int(text, 16)
    return text


@dataclass(frozen=True)
class ObservationSignal:
    """Adapter-agnostic inbound value signal (RPC / indexer / explorer)."""

    chain: str
    tx_hash: str
    log_index: int
    to_address: str
    amount: Decimal
    token_contract: str
    confirmations: int
    from_address: str = ""
    block_number: int | None = None


class DepositObservationService:
    """Idempotent Observation Identity ingest and Confirmation Policy apply.

    Confirmed does **not** call CreditService (Cap 3).
    """

    def __init__(self, confirmation_policy: ConfirmationPolicy | None = None) -> None:
        self.confirmation_policy = confirmation_policy or ConfirmationPolicy()

    def ingest(
        self, signal: ObservationSignal, *, shadow_only: bool = False
    ) -> DepositObservation:
        chain = signal.chain or WalletChain.POLYGON
        tx_hash = normalize_tx_hash(signal.tx_hash)
        to_address = normalize_evm_address(signal.to_address)
        token = normalize_evm_address(signal.token_contract)
        if signal.from_address:
            from_address = normalize_evm_address(signal.from_address)
        else:
            from_address = ""
        amount = Decimal(signal.amount)

        wallet_address = (
            WalletAddress.objects.filter(chain=chain, address=to_address)
            .order_by("-created_at")
            .first()
        )
        if wallet_address is None:
            raise ObservationAttributionError(
                f"no WalletAddress for chain={chain} address={to_address}"
            )

        for _ in range(5):
            try:
                with transaction.atomic():
                    return self._ingest_once(
                        wallet_address=wallet_address,
                        chain=chain,
                        tx_hash=tx_hash,
                        log_index=signal.log_index,
                        amount=amount,
                        token_contract=token,
                        from_address=from_address,
                        confirmations=int(signal.confirmations),
                        block_number=signal.block_number,
                        shadow_only=shadow_only,
                    )
            except IntegrityError:
                continue

        existing = DepositObservation.objects.get(
            chain=chain, tx_hash=tx_hash, log_index=signal.log_index
        )
        return self._refresh_locked(existing, confirmations=int(signal.confirmations))

    def advance_confirmation(
        self, observation: DepositObservation
    ) -> DepositObservation:
        """Re-apply Confirmation Policy to a non-terminal Observation."""
        with transaction.atomic():
            locked = DepositObservation.objects.select_for_update().get(
                pk=observation.pk
            )
            return self._apply_policy(locked)

    def expire(self, observation: DepositObservation) -> DepositObservation:
        """Mark Pending/Observed as Expired (Observation Window timeout)."""
        with transaction.atomic():
            locked = DepositObservation.objects.select_for_update().get(
                pk=observation.pk
            )
            if locked.status in _TERMINAL:
                if locked.status == ObservationStatus.EXPIRED:
                    return locked
                raise ObservationTransitionError(
                    f"cannot expire observation in status={locked.status}"
                )
            locked.status = ObservationStatus.EXPIRED
            locked.expired_at = timezone.now()
            locked.status_reason = locked.status_reason or "observation_window_timeout"
            locked.save(
                update_fields=["status", "expired_at", "status_reason", "updated_at"]
            )
            return locked

    def _ingest_once(
        self,
        *,
        wallet_address: WalletAddress,
        chain: str,
        tx_hash: str,
        log_index: int,
        amount: Decimal,
        token_contract: str,
        from_address: str,
        confirmations: int,
        block_number: int | None,
        shadow_only: bool = False,
    ) -> DepositObservation:
        existing = (
            DepositObservation.objects.select_for_update()
            .filter(chain=chain, tx_hash=tx_hash, log_index=log_index)
            .first()
        )
        if existing is not None:
            return self._refresh_locked(existing, confirmations=confirmations)

        obs = DepositObservation.objects.create(
            wallet_address=wallet_address,
            chain=chain,
            tx_hash=tx_hash,
            log_index=log_index,
            amount=amount,
            token_contract=token_contract,
            from_address=from_address,
            confirmations=confirmations,
            block_number=block_number,
            status=ObservationStatus.OBSERVED,
            shadow_only=shadow_only,
        )
        obs.status = ObservationStatus.PENDING_CONFIRMATION
        obs.pending_at = timezone.now()
        obs.save(update_fields=["status", "pending_at", "updated_at"])
        return self._apply_policy(obs)

    def _refresh_locked(
        self,
        observation: DepositObservation,
        *,
        confirmations: int,
    ) -> DepositObservation:
        if observation.status in _TERMINAL:
            return observation
        if confirmations > observation.confirmations:
            observation.confirmations = confirmations
            observation.save(update_fields=["confirmations", "updated_at"])
        if observation.status == ObservationStatus.OBSERVED:
            observation.status = ObservationStatus.PENDING_CONFIRMATION
            observation.pending_at = timezone.now()
            observation.save(update_fields=["status", "pending_at", "updated_at"])
        return self._apply_policy(observation)

    def _apply_policy(self, observation: DepositObservation) -> DepositObservation:
        if observation.status in _TERMINAL:
            return observation
        if observation.status not in {
            ObservationStatus.OBSERVED,
            ObservationStatus.PENDING_CONFIRMATION,
        }:
            return observation

        result = self.confirmation_policy.evaluate(observation)
        if result.outcome == ConfirmationOutcome.PENDING:
            if observation.status != ObservationStatus.PENDING_CONFIRMATION:
                observation.status = ObservationStatus.PENDING_CONFIRMATION
                observation.pending_at = observation.pending_at or timezone.now()
            observation.status_reason = result.reason
            observation.save(
                update_fields=["status", "pending_at", "status_reason", "updated_at"]
            )
            return observation

        if result.outcome == ConfirmationOutcome.REJECTED:
            observation.status = ObservationStatus.REJECTED
            observation.rejected_at = timezone.now()
            observation.status_reason = result.reason
            observation.save(
                update_fields=[
                    "status",
                    "rejected_at",
                    "status_reason",
                    "updated_at",
                ]
            )
            return observation

        observation.status = ObservationStatus.CONFIRMED
        observation.confirmed_at = timezone.now()
        observation.status_reason = ""
        observation.save(
            update_fields=["status", "confirmed_at", "status_reason", "updated_at"]
        )
        return observation
