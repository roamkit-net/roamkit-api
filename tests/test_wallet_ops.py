"""Wallet Operations drills (ADR 017 Cap — Wallet Operations)."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from apps.wallet.models import (
    ObservationStatus,
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)
from apps.wallet.services import (
    DepositObservationService,
    ObservationSignal,
    collect_wallet_ops_status,
    resume_converts,
)

User = get_user_model()

_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_TX = "0x" + "bb" * 32


def _address(*, email: str, index: int) -> WalletAddress:
    user = User.objects.create_user(email=email, password="secret123")
    identity = WalletIdentity.objects.create(account=user.billing_account)
    return WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address=f"0x{index:040d}",
        derivation_index=index,
        status=WalletAddressStatus.ACTIVE,
    )


@pytest.mark.django_db
@override_settings(
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
    WALLET_HD_MNEMONIC="test test test test test test test test test test test junk",
)
def test_ops_status_counts_and_seed_flag() -> None:
    addr = _address(email="ops-status@example.com", index=0)
    DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=0,
            to_address=addr.address,
            amount=Decimal("3.000000"),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    status = collect_wallet_ops_status()
    assert status.seed_configured is True
    assert status.wallet_address_count == 1
    assert status.confirmed_awaiting_convert == 1
    assert status.observation_counts[ObservationStatus.CONFIRMED] == 1


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_resume_converts_dry_run_does_not_credit() -> None:
    addr = _address(email="ops-dry@example.com", index=1)
    obs = DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=1,
            to_address=addr.address,
            amount=Decimal("7.000000"),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    report = resume_converts(apply=False)
    assert report["candidates"] == 1
    assert report["credited"] == 0
    obs.refresh_from_db()
    assert obs.status == ObservationStatus.CONFIRMED
    account = addr.wallet_identity.account
    account.refresh_from_db()
    assert account.balance == Decimal("0")


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_resume_converts_apply_credits_once() -> None:
    addr = _address(email="ops-apply@example.com", index=2)
    obs = DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=2,
            to_address=addr.address,
            amount=Decimal("9.250000"),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    first = resume_converts(apply=True)
    second = resume_converts(apply=True)
    assert first["credited"] == 1
    assert second["candidates"] == 0
    obs.refresh_from_db()
    assert obs.status == ObservationStatus.CREDITED
    account = addr.wallet_identity.account
    account.refresh_from_db()
    assert account.balance == Decimal("9.250000")


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_wallet_ops_status_command() -> None:
    out = StringIO()
    call_command("wallet_ops_status", stdout=out)
    text = out.getvalue()
    assert "wallet_address_count=" in text
    assert "seed_configured=" in text


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_wallet_resume_converts_command_dry_run() -> None:
    addr = _address(email="ops-cmd@example.com", index=3)
    DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=3,
            to_address=addr.address,
            amount=Decimal("1.000000"),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    out = StringIO()
    call_command("wallet_resume_converts", stdout=out)
    assert "mode=dry-run" in out.getvalue()
    assert "candidates=1" in out.getvalue()
