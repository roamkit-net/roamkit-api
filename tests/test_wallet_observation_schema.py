"""Model tests for DepositObservation schema (RFC 006 Cap 2).

Implements:
ADR 017
RFC 006
Capability: Deposit Observation (schema)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)


def _active_address(*, email: str, index: int = 0) -> WalletAddress:
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
def test_observation_identity_unique() -> None:
    addr = _active_address(email="obs-unique@example.com", index=0)
    DepositObservation.objects.create(
        wallet_address=addr,
        chain=WalletChain.POLYGON,
        tx_hash="0x" + "ab" * 32,
        log_index=0,
        amount=Decimal("10.000000"),
        token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        status=ObservationStatus.OBSERVED,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        DepositObservation.objects.create(
            wallet_address=addr,
            chain=WalletChain.POLYGON,
            tx_hash="0x" + "ab" * 32,
            log_index=0,
            amount=Decimal("1.000000"),
            token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        )


@pytest.mark.django_db
def test_same_tx_different_log_index_allowed() -> None:
    addr = _active_address(email="obs-logs@example.com", index=1)
    tx = "0x" + "cd" * 32
    a = DepositObservation.objects.create(
        wallet_address=addr,
        chain=WalletChain.POLYGON,
        tx_hash=tx,
        log_index=0,
        amount=Decimal("1.000000"),
        token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    )
    b = DepositObservation.objects.create(
        wallet_address=addr,
        chain=WalletChain.POLYGON,
        tx_hash=tx,
        log_index=1,
        amount=Decimal("2.000000"),
        token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    )
    assert a.pk != b.pk


@pytest.mark.django_db
def test_observation_amount_must_be_positive() -> None:
    addr = _active_address(email="obs-amt@example.com", index=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        DepositObservation.objects.create(
            wallet_address=addr,
            chain=WalletChain.POLYGON,
            tx_hash="0x" + "ef" * 32,
            log_index=0,
            amount=Decimal("0"),
            token_contract="0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        )


@pytest.mark.django_db
def test_observation_status_choices_cover_rfc006_sm() -> None:
    values = {c.value for c in ObservationStatus}
    assert values == {
        "observed",
        "pending_confirmation",
        "confirmed",
        "conversion_started",
        "credited",
        "rejected",
        "expired",
    }
