"""ADR 018 Phase 1 Shadow dual-path tests (Cutover PR3)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.billing.models import CreditLedgerEntry, DepositRequest
from apps.billing.services.deposit_verification import DepositVerificationService
from apps.wallet.exceptions import ObservationTransitionError
from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    ShadowDecision,
    ShadowDecisionOutcome,
    ShadowDecisionSeverity,
)
from apps.wallet.services import (
    CreditConversionService,
    WalletAllocationService,
    collect_wallet_metrics,
)
from apps.wallet.services.conversion import observation_idempotency_key
from shared.events.billing_events import CreditGranted, DepositVerified
from shared.events.event_bus import event_bus
from shared.providers.blockchain import TransferResult

User = get_user_model()

_MNEMONIC = "test test test test test test test test test test test junk"
_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_TX = "0x" + ("ab" * 32)


class _FakeBlockchainProvider:
    def __init__(self, result: TransferResult) -> None:
        self._result = result

    def fetch_usdt_transfer(self, tx_hash: str) -> TransferResult:
        return self._result


def _transfer(
    *,
    amount: Decimal = Decimal("10.000000"),
    confirmations: int = 30,
    to_address: str = "0x2222222222222222222222222222222222222222",
) -> TransferResult:
    return TransferResult(
        tx_hash=_TX,
        from_address="0x1111111111111111111111111111111111111111",
        to_address=to_address,
        amount=amount,
        confirmations=confirmations,
        block_number=100,
        token_contract=_USDT,
        status="success",
        raw_rpc_response={
            "receipt": {"status": "0x1"},
            "matched_log": {"logIndex": "0x0"},
            "tx_hash": _TX,
        },
    )


@pytest.fixture
def account(db):
    user = User.objects.create_user(email="shadow@example.com", password="secret123")
    return user.billing_account


@pytest.fixture
def collected_events():
    received: list[object] = []

    def _capture_deposit(event: DepositVerified) -> None:
        received.append(event)

    def _capture_credit(event: CreditGranted) -> None:
        received.append(event)

    event_bus.subscribe(DepositVerified, _capture_deposit)
    event_bus.subscribe(CreditGranted, _capture_credit)
    try:
        yield received
    finally:
        event_bus._handlers[DepositVerified].remove(_capture_deposit)
        event_bus._handlers[CreditGranted].remove(_capture_credit)


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SHADOW_MODE=False,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_shadow_off_skips_decision_record(account, collected_events) -> None:
    WalletAllocationService().ensure_active_address(account)
    svc = DepositVerificationService(
        blockchain_provider=_FakeBlockchainProvider(_transfer()),  # type: ignore[arg-type]
        min_confirmations=20,
    )
    deposit = svc.verify(
        account,
        tx_hash=_TX,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="shadow-off-1",
    )
    assert deposit.status == DepositRequest.Status.COMPLETED
    assert ShadowDecision.objects.count() == 0
    assert DepositObservation.objects.count() == 0
    assert sum(1 for e in collected_events if isinstance(e, CreditGranted)) == 1


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SHADOW_MODE=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_shadow_on_match_no_wallet_obs_credit(account, collected_events) -> None:
    WalletAllocationService().ensure_active_address(account)
    amount = Decimal("15.500000")
    svc = DepositVerificationService(
        blockchain_provider=_FakeBlockchainProvider(_transfer(amount=amount)),  # type: ignore[arg-type]
        min_confirmations=20,
    )
    deposit = svc.verify(
        account,
        tx_hash=_TX,
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        amount_requested=amount,
        idempotency_key="shadow-on-1",
    )
    assert deposit.status == DepositRequest.Status.COMPLETED

    decision = ShadowDecision.objects.get(deposit_request=deposit)
    assert decision.outcome == ShadowDecisionOutcome.EQUAL
    assert decision.severity == ShadowDecisionSeverity.NONE
    assert decision.reason == "match"
    assert decision.shadow_would_credit is True
    assert decision.observation is not None
    assert decision.observation.shadow_only is True
    assert decision.observation.status == ObservationStatus.CONFIRMED

    # Sole production credit is legacy deposit:{id}
    assert (
        CreditLedgerEntry.objects.filter(
            idempotency_key=f"deposit:{deposit.pk}"
        ).count()
        == 1
    )
    assert not CreditLedgerEntry.objects.filter(
        idempotency_key=observation_idempotency_key(decision.observation)
    ).exists()
    assert sum(1 for e in collected_events if isinstance(e, CreditGranted)) == 1

    metrics = collect_wallet_metrics()
    assert metrics.shadow_match_total == 1
    assert metrics.shadow_mismatch_total == 0
    assert metrics.shadow_match_rate == 1.0
    assert metrics.shadow_latency_ms_avg is not None


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SHADOW_MODE=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_shadow_missing_wallet_is_critical(account) -> None:
    svc = DepositVerificationService(
        blockchain_provider=_FakeBlockchainProvider(_transfer()),  # type: ignore[arg-type]
        min_confirmations=20,
    )
    deposit = svc.verify(
        account,
        tx_hash=_TX,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="shadow-miss-1",
    )
    decision = ShadowDecision.objects.get(deposit_request=deposit)
    assert decision.outcome == ShadowDecisionOutcome.DIFFERENT
    assert decision.severity == ShadowDecisionSeverity.CRITICAL
    assert decision.reason == "missing_wallet_address"
    assert DepositObservation.objects.count() == 0


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SHADOW_MODE=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_shadow_only_observation_cannot_convert(account) -> None:
    WalletAllocationService().ensure_active_address(account)
    svc = DepositVerificationService(
        blockchain_provider=_FakeBlockchainProvider(_transfer()),  # type: ignore[arg-type]
        min_confirmations=20,
    )
    deposit = svc.verify(
        account,
        tx_hash=_TX,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="shadow-noconvert-1",
    )
    obs = ShadowDecision.objects.get(deposit_request=deposit).observation
    assert obs is not None
    with pytest.raises(ObservationTransitionError, match="shadow_only"):
        CreditConversionService().convert(obs)
    assert not CreditLedgerEntry.objects.filter(
        idempotency_key=observation_idempotency_key(obs)
    ).exists()


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SHADOW_MODE=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_shadow_failure_does_not_break_legacy(account, monkeypatch) -> None:
    WalletAllocationService().ensure_active_address(account)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("shadow boom")

    monkeypatch.setattr(
        "apps.wallet.services.shadow.compare_legacy_deposit",
        _boom,
    )
    svc = DepositVerificationService(
        blockchain_provider=_FakeBlockchainProvider(_transfer()),  # type: ignore[arg-type]
        min_confirmations=20,
    )
    deposit = svc.verify(
        account,
        tx_hash=_TX,
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        amount_requested=Decimal("10.000000"),
        idempotency_key="shadow-boom-1",
    )
    assert deposit.status == DepositRequest.Status.COMPLETED
    account.refresh_from_db()
    assert account.balance == Decimal("10.000000")


@pytest.mark.django_db
def test_shadow_module_does_not_import_credit_service() -> None:
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "apps"
        / "wallet"
        / "services"
        / "shadow.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "CreditService" not in {a.name for a in node.names}
            assert "credit_service" not in {a.name for a in node.names}
            if "billing" in node.module:
                assert "credit" not in node.module
