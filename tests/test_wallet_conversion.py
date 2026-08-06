"""Credit Conversion service tests (ADR 017 / RFC 006 Cap 3).

Implements:
ADR 017
RFC 006
Capability: Credit Conversion
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.wallet.exceptions import ObservationTransitionError
from apps.wallet.models import (
    ObservationStatus,
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)
from apps.wallet.services import (
    CreditConversionService,
    DepositObservationService,
    ObservationSignal,
)

User = get_user_model()

_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_TX = "0x" + "aa" * 32


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


def _confirmed(*, email: str, index: int, amount: str = "25.500000"):
    addr = _address(email=email, index=index)
    obs = DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=0,
            to_address=addr.address,
            amount=Decimal(amount),
            token_contract=_USDT,
            confirmations=50,
        )
    )
    assert obs.status == ObservationStatus.CONFIRMED
    return obs


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_convert_credits_account_via_credit_service() -> None:
    obs = _confirmed(email="conv-ok@example.com", index=0, amount="25.500000")
    account = obs.wallet_address.wallet_identity.account
    assert account.balance == Decimal("0")

    entry = CreditConversionService().convert(obs)
    account.refresh_from_db()
    obs.refresh_from_db()

    assert obs.status == ObservationStatus.CREDITED
    assert obs.credited_at is not None
    assert account.balance == Decimal("25.500000")
    assert entry.reference_type == LedgerReferenceType.DEPOSIT
    assert entry.reference_id == str(obs.pk)
    assert entry.delta == Decimal("25.500000")


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_convert_idempotent_on_observation_identity() -> None:
    obs = _confirmed(email="conv-idem@example.com", index=1)
    svc = CreditConversionService()
    a = svc.convert(obs)
    b = svc.convert(obs)
    assert a.pk == b.pk
    assert CreditLedgerEntry.objects.count() == 1
    account = obs.wallet_address.wallet_identity.account
    account.refresh_from_db()
    assert account.balance == Decimal("25.500000")


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_convert_rejects_pending_observation() -> None:
    addr = _address(email="conv-pend@example.com", index=2)
    obs = DepositObservationService().ingest(
        ObservationSignal(
            chain=WalletChain.POLYGON,
            tx_hash=_TX,
            log_index=3,
            to_address=addr.address,
            amount=Decimal("1.000000"),
            token_contract=_USDT,
            confirmations=1,
        )
    )
    assert obs.status == ObservationStatus.PENDING_CONFIRMATION
    with pytest.raises(ObservationTransitionError):
        CreditConversionService().convert(obs)
    addr.wallet_identity.account.refresh_from_db()
    assert addr.wallet_identity.account.balance == Decimal("0")


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_confirmed_ingest_alone_does_not_credit() -> None:
    obs = _confirmed(email="conv-noauto@example.com", index=3)
    account = obs.wallet_address.wallet_identity.account
    account.refresh_from_db()
    assert account.balance == Decimal("0")
    assert obs.status == ObservationStatus.CONFIRMED
