"""Binance Funding Provider adapter tests (ADR 017 / RFC 005 Cap 5).

Implements:
ADR 017
RFC 005
Capability: Funding Provider — Binance
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.integrations.binance.providers import BinanceFundingProvider
from apps.wallet.models import WalletAddressStatus
from apps.wallet.services import FundingService, WalletAllocationService
from shared.providers.funding import (
    FundingDepositRequest,
    FundingProviderError,
    FundingProviderUnsupportedError,
)

User = get_user_model()

_TEST_MNEMONIC = "test test test test test test test test test test test junk"
_BINANCE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "apps" / "integrations" / "binance"
)


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_binance_deposit_guides_to_active_wallet_address() -> None:
    user = User.objects.create_user(
        email="binance-dep@example.com", password="secret123"
    )
    guide = FundingService().start_deposit(user.billing_account, provider_id="binance")

    assert guide.provider_id == "binance"
    assert guide.funding_source == "exchange_withdrawal"
    assert guide.asset == "USDT"
    assert guide.chain == "polygon"
    assert guide.network_label == "MATIC"
    assert guide.destination_address.startswith("0x")
    assert guide.session_id.startswith("binance:v1:")
    assert "Credits" in guide.instructions
    assert "on-chain" in guide.instructions.lower()


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_binance_status_is_ux_only_awaiting_deposit() -> None:
    user = User.objects.create_user(
        email="binance-st@example.com", password="secret123"
    )
    svc = FundingService()
    guide = svc.start_deposit(user.billing_account, provider_id="binance")
    status = svc.status("binance", guide.session_id)

    assert status.provider_id == "binance"
    assert status.state == "awaiting_deposit"
    assert status.destination_address == guide.destination_address
    assert "Credits" in status.message


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_binance_rejects_non_usdt_asset() -> None:
    user = User.objects.create_user(
        email="binance-asset@example.com", password="secret123"
    )
    addr = WalletAllocationService().ensure_active_address(user.billing_account)
    with pytest.raises(FundingProviderUnsupportedError):
        BinanceFundingProvider().deposit(
            FundingDepositRequest(
                wallet_address_id=addr.pk,
                destination_address=addr.address,
                chain="polygon",
                asset="ETH",
            )
        )


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_binance_deposit_requires_active_address() -> None:
    user = User.objects.create_user(
        email="binance-retired@example.com", password="secret123"
    )
    alloc = WalletAllocationService()
    first = alloc.ensure_active_address(user.billing_account)
    alloc.rotate_active_address(user.billing_account)
    first.refresh_from_db()
    assert first.status == WalletAddressStatus.RETIRED

    with pytest.raises(FundingProviderError):
        BinanceFundingProvider().deposit(
            FundingDepositRequest(
                wallet_address_id=first.pk,
                destination_address=first.address,
                chain="polygon",
                asset="USDT",
            )
        )


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_binance_start_deposit_does_not_change_balance() -> None:
    user = User.objects.create_user(
        email="binance-bal@example.com", password="secret123"
    )
    account = user.billing_account
    before = account.balance
    FundingService().start_deposit(account, provider_id="binance")
    account.refresh_from_db()
    assert account.balance == before


def test_binance_modules_do_not_import_credit_service() -> None:
    offenders: list[str] = []
    for path in sorted(_BINANCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if "billing" in node.module and "credit" in node.module:
                offenders.append(str(path))
    assert not offenders


def test_binance_metadata_lists_polygon_usdt() -> None:
    meta = BinanceFundingProvider().metadata()
    assert meta.provider_id == "binance"
    assert "USDT" in meta.assets
    assert "polygon" in meta.chains
    assert set(meta.capabilities) >= {"deposit", "status", "metadata"}


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_mexc_and_binance_are_interchangeable_via_funding_service() -> None:
    user = User.objects.create_user(
        email="both-providers@example.com", password="secret123"
    )
    svc = FundingService()
    mexc = svc.start_deposit(user.billing_account, provider_id="mexc")
    binance = svc.start_deposit(user.billing_account, provider_id="binance")
    assert mexc.destination_address == binance.destination_address
    assert mexc.provider_id == "mexc"
    assert binance.provider_id == "binance"
