"""Wallet Metrics tests (ADR 017 Cap — Wallet Metrics)."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from apps.wallet.models import WalletAddressStatus, WalletChain
from apps.wallet.services import (
    CreditConversionService,
    DepositObservationService,
    ObservationSignal,
    WalletAllocationService,
    collect_wallet_metrics,
)

User = get_user_model()

_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_MNEMONIC = "test test test test test test test test test test test junk"
_TX = "0x" + "cc" * 32


@pytest.mark.django_db
@override_settings(
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
)
def test_collect_wallet_metrics_allocation_obs_convert() -> None:
    user = User.objects.create_user(email="metrics@example.com", password="secret123")
    addr = WalletAllocationService().ensure_active_address(user.billing_account)
    assert addr.status == WalletAddressStatus.ACTIVE

    obs = DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=0,
            to_address=addr.address,
            amount=Decimal("12.500000"),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    CreditConversionService().convert(obs)

    metrics = collect_wallet_metrics()
    assert metrics.wallet_identity_count == 1
    assert metrics.wallet_address_active == 1
    assert metrics.wallet_address_retired == 0
    assert metrics.derivation_index_max == 0
    assert metrics.credited_count == 1
    assert metrics.credited_amount_total == Decimal("12.500000")
    assert metrics.confirmed_awaiting_convert == 0
    assert metrics.pending_confirmation == 0


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_wallet_metrics_command() -> None:
    out = StringIO()
    call_command("wallet_metrics", stdout=out)
    text = out.getvalue()
    assert "wallet_identity_count=" in text
    assert "credited_amount_total=" in text
    assert "pending_confirmation=" in text
    assert "shadow_match_total=" in text
    assert "shadow_mismatch_total=" in text
    assert "shadow_match_rate=" in text


@pytest.mark.django_db
def test_wallet_metrics_module_does_not_import_credit_service() -> None:
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "apps"
        / "wallet"
        / "services"
        / "metrics.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not ("billing" in node.module and "credit" in node.module)
