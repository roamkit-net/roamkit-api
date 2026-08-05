"""Confirmed Observation → Credits (ADR 017 / RFC 006 Cap 3)."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.services import credit_service
from apps.wallet.exceptions import ObservationTransitionError
from apps.wallet.models import DepositObservation, ObservationStatus


def observation_idempotency_key(observation: DepositObservation) -> str:
    """Stable CreditService idempotency key from Observation Identity."""
    return (
        f"wallet-obs:{observation.chain}:{observation.tx_hash}:"
        f"{observation.log_index}"
    )


class CreditConversionService:
    """Handoff Confirmed Observation → CreditService → Credited.

    Only Confirmed Observations may convert. Idempotent on Observation Identity.
    """

    def convert(self, observation: DepositObservation) -> CreditLedgerEntry:
        with transaction.atomic():
            locked = DepositObservation.objects.select_for_update().get(
                pk=observation.pk
            )
            if locked.status == ObservationStatus.CREDITED:
                entry = CreditLedgerEntry.objects.filter(
                    idempotency_key=observation_idempotency_key(locked)
                ).first()
                if entry is None:
                    raise ObservationTransitionError(
                        "observation CREDITED but ledger entry missing"
                    )
                return entry

            if locked.shadow_only:
                raise ObservationTransitionError(
                    "shadow_only observation cannot convert (ADR 018 Phase 1)"
                )

            if locked.status == ObservationStatus.CONVERSION_STARTED:
                # Resume after crash between start and credit.
                return self._credit_and_finish(locked)

            if locked.status != ObservationStatus.CONFIRMED:
                raise ObservationTransitionError(
                    f"convert requires CONFIRMED, got {locked.status}"
                )

            locked.status = ObservationStatus.CONVERSION_STARTED
            locked.conversion_started_at = timezone.now()
            locked.save(update_fields=["status", "conversion_started_at", "updated_at"])
            return self._credit_and_finish(locked)

    def _credit_and_finish(self, observation: DepositObservation) -> CreditLedgerEntry:
        account = observation.wallet_address.wallet_identity.account
        entry = credit_service.credit(
            account,
            observation.amount,
            reference_type=LedgerReferenceType.DEPOSIT,
            reference_id=observation.pk,
            idempotency_key=observation_idempotency_key(observation),
        )
        if observation.status != ObservationStatus.CREDITED:
            observation.status = ObservationStatus.CREDITED
            observation.credited_at = timezone.now()
            observation.status_reason = ""
            observation.save(
                update_fields=[
                    "status",
                    "credited_at",
                    "status_reason",
                    "updated_at",
                ]
            )
        return entry
