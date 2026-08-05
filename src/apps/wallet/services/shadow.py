"""ADR 018 Phase 1 Shadow dual-path (compare only — never grants Credits).

Legacy ADR 010 remains the sole production money path. When ``SHADOW_MODE`` is
on, each completed deposit also runs Wallet Observation + dry shadow conversion
and writes a ``ShadowDecision`` record.

Stop criteria (ops — halt Phase 1 advancement):
- duplicate credit (must never happen; shadow never calls CreditService)
- shadow would grant production Credits (guarded by ``shadow_only``)
- shadow failures must not affect legacy latency / success

Does not change ``deposit-info`` or flip WalletAddress as default.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import transaction

from apps.billing.models import Account, CreditLedgerEntry, DepositRequest
from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    ShadowDecision,
    ShadowDecisionOutcome,
    ShadowDecisionSeverity,
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
)
from apps.wallet.services.conversion import observation_idempotency_key
from apps.wallet.services.flags import get_cutover_flags
from apps.wallet.services.observation import (
    DepositObservationService,
    ObservationSignal,
)
from shared.providers.blockchain import TransferResult

logger = logging.getLogger(__name__)

_MONEY_QUANT = Decimal("0.000001")


def _log_index_from_transfer(transfer: TransferResult) -> int:
    matched = (transfer.raw_rpc_response or {}).get("matched_log") or {}
    raw = matched.get("logIndex", matched.get("index", 0))
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text.startswith("0x"):
            return int(text, 16)
        return int(text)
    return int(raw or 0)


def _active_wallet_address(account: Account, *, chain: str) -> WalletAddress | None:
    return (
        WalletAddress.objects.filter(
            wallet_identity__account=account,
            chain=chain,
            status=WalletAddressStatus.ACTIVE,
        )
        .order_by("derivation_index")
        .first()
    )


def compare_legacy_deposit(
    *,
    deposit: DepositRequest,
    transfer: TransferResult,
) -> ShadowDecision | None:
    """Run shadow dual-path for a completed legacy deposit.

    No-op when ``SHADOW_MODE`` is off. Never raises to the caller — isolated
    failures are persisted as ``ERROR`` / Critical when possible.
    """
    flags = get_cutover_flags()
    if not flags.shadow_mode:
        return None

    existing = ShadowDecision.objects.filter(deposit_request=deposit).first()
    if existing is not None:
        return existing

    started = time.perf_counter()
    try:
        return _compare_once(deposit=deposit, transfer=transfer, started=started)
    except Exception as exc:  # noqa: BLE001 — never break ADR 010 path
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        logger.exception(
            "wallet shadow dual-path failed deposit_id=%s",
            deposit.pk,
        )
        return _persist_error(
            deposit=deposit,
            transfer=transfer,
            reason=f"shadow_pipeline_error:{exc}"[:255],
            latency_ms=latency_ms,
        )


def _compare_once(
    *,
    deposit: DepositRequest,
    transfer: TransferResult,
    started: float,
) -> ShadowDecision:
    chain = WalletChain.POLYGON
    legacy_amount = Decimal(deposit.amount_credited or transfer.amount).quantize(
        _MONEY_QUANT
    )
    legacy_account_id = deposit.account_id
    legacy_tx_hash = (deposit.tx_hash or transfer.tx_hash or "").lower()

    wallet_address = _active_wallet_address(deposit.account, chain=chain)
    if wallet_address is None:
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        return ShadowDecision.objects.create(
            deposit_request=deposit,
            observation=None,
            legacy_amount=legacy_amount,
            legacy_account_id=legacy_account_id,
            legacy_tx_hash=legacy_tx_hash,
            shadow_amount=None,
            shadow_account_id=None,
            shadow_observation_status="",
            shadow_would_credit=False,
            outcome=ShadowDecisionOutcome.DIFFERENT,
            severity=ShadowDecisionSeverity.CRITICAL,
            reason="missing_wallet_address",
            latency_ms=latency_ms,
        )

    signal = ObservationSignal(
        chain=chain,
        tx_hash=legacy_tx_hash,
        log_index=_log_index_from_transfer(transfer),
        # Shadow attribution: bind Observation to Account WalletAddress even
        # though on-chain ``to`` is still the ADR 010 shared platform wallet.
        to_address=wallet_address.address,
        amount=transfer.amount,
        token_contract=transfer.token_contract,
        confirmations=int(transfer.confirmations),
        from_address=transfer.from_address or "",
        block_number=transfer.block_number,
    )
    observation = DepositObservationService().ingest(signal, shadow_only=True)

    shadow_account_id = observation.wallet_address.wallet_identity.account_id
    shadow_amount = Decimal(observation.amount).quantize(_MONEY_QUANT)
    would_credit = observation.status == ObservationStatus.CONFIRMED

    # Dry shadow conversion: never call CreditConversionService / CreditService.
    # Detect accidental production credit under the wallet-obs key.
    wallet_ledger = CreditLedgerEntry.objects.filter(
        idempotency_key=observation_idempotency_key(observation)
    ).exists()
    if wallet_ledger:
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        return ShadowDecision.objects.create(
            deposit_request=deposit,
            observation=observation,
            legacy_amount=legacy_amount,
            legacy_account_id=legacy_account_id,
            legacy_tx_hash=legacy_tx_hash,
            shadow_amount=shadow_amount,
            shadow_account_id=shadow_account_id,
            shadow_observation_status=observation.status,
            shadow_would_credit=would_credit,
            outcome=ShadowDecisionOutcome.DIFFERENT,
            severity=ShadowDecisionSeverity.CRITICAL,
            reason="duplicate_credit_wallet_obs_ledger_present",
            latency_ms=latency_ms,
        )

    outcome, severity, reason = _classify(
        legacy_amount=legacy_amount,
        legacy_account_id=legacy_account_id,
        shadow_amount=shadow_amount,
        shadow_account_id=shadow_account_id,
        observation=observation,
        would_credit=would_credit,
    )
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    return ShadowDecision.objects.create(
        deposit_request=deposit,
        observation=observation,
        legacy_amount=legacy_amount,
        legacy_account_id=legacy_account_id,
        legacy_tx_hash=legacy_tx_hash,
        shadow_amount=shadow_amount,
        shadow_account_id=shadow_account_id,
        shadow_observation_status=observation.status,
        shadow_would_credit=would_credit,
        outcome=outcome,
        severity=severity,
        reason=reason,
        latency_ms=latency_ms,
    )


def _classify(
    *,
    legacy_amount: Decimal,
    legacy_account_id: UUID,
    shadow_amount: Decimal,
    shadow_account_id: UUID,
    observation: DepositObservation,
    would_credit: bool,
) -> tuple[str, str, str]:
    if observation.status == ObservationStatus.REJECTED:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.CRITICAL,
            f"shadow_rejected:{observation.status_reason or 'rejected'}"[:255],
        )
    if observation.status in {
        ObservationStatus.PENDING_CONFIRMATION,
        ObservationStatus.OBSERVED,
    }:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.WARNING,
            "observation_pending_confirmation",
        )
    if observation.status == ObservationStatus.EXPIRED:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.WARNING,
            "observation_expired",
        )
    if shadow_account_id != legacy_account_id:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.CRITICAL,
            "account_mismatch",
        )
    if shadow_amount != legacy_amount:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.CRITICAL,
            "amount_mismatch",
        )
    if not would_credit:
        return (
            ShadowDecisionOutcome.DIFFERENT,
            ShadowDecisionSeverity.WARNING,
            f"shadow_not_confirmed:{observation.status}",
        )
    return (
        ShadowDecisionOutcome.EQUAL,
        ShadowDecisionSeverity.NONE,
        "match",
    )


def _persist_error(
    *,
    deposit: DepositRequest,
    transfer: TransferResult,
    reason: str,
    latency_ms: int,
) -> ShadowDecision | None:
    try:
        with transaction.atomic():
            existing = ShadowDecision.objects.filter(deposit_request=deposit).first()
            if existing is not None:
                return existing
            return ShadowDecision.objects.create(
                deposit_request=deposit,
                observation=None,
                legacy_amount=Decimal(
                    deposit.amount_credited or transfer.amount or 0
                ).quantize(_MONEY_QUANT),
                legacy_account_id=deposit.account_id,
                legacy_tx_hash=(deposit.tx_hash or transfer.tx_hash or "").lower(),
                shadow_amount=None,
                shadow_account_id=None,
                shadow_observation_status="",
                shadow_would_credit=False,
                outcome=ShadowDecisionOutcome.ERROR,
                severity=ShadowDecisionSeverity.CRITICAL,
                reason=reason,
                latency_ms=latency_ms,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "wallet shadow failed to persist ERROR decision deposit_id=%s",
            deposit.pk,
        )
        return None


def safe_compare_legacy_deposit(
    *,
    deposit: DepositRequest,
    transfer: TransferResult,
) -> None:
    """Billing hook: never raise; never grant Credits."""
    try:
        compare_legacy_deposit(deposit=deposit, transfer=transfer)
    except Exception:  # noqa: BLE001 — belt-and-suspenders around ADR 010
        logger.exception(
            "wallet shadow safe_compare failed deposit_id=%s",
            deposit.pk,
        )


def shadow_metrics_snapshot() -> dict[str, Any]:
    """First-class cutover counters for Phase 1 / Phase 2 KPI (match_rate)."""
    match = ShadowDecision.objects.filter(outcome=ShadowDecisionOutcome.EQUAL).count()
    mismatch = ShadowDecision.objects.filter(
        outcome=ShadowDecisionOutcome.DIFFERENT
    ).count()
    error = ShadowDecision.objects.filter(outcome=ShadowDecisionOutcome.ERROR).count()
    critical = ShadowDecision.objects.filter(
        severity=ShadowDecisionSeverity.CRITICAL
    ).count()
    warning = ShadowDecision.objects.filter(
        severity=ShadowDecisionSeverity.WARNING
    ).count()
    decided = match + mismatch + error
    match_rate = None
    if decided:
        match_rate = float(match) / float(decided)
    latency_rows = ShadowDecision.objects.values_list("latency_ms", flat=True)
    latency_list = list(latency_rows)
    avg_latency = int(sum(latency_list) / len(latency_list)) if latency_list else None
    return {
        "shadow_match_total": match,
        "shadow_mismatch_total": mismatch,
        "shadow_error_total": error,
        "shadow_critical_total": critical,
        "shadow_warning_total": warning,
        "shadow_match_rate": match_rate,
        "shadow_latency_ms_avg": avg_latency,
    }
