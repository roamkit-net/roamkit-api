"""Deposit Observation service tests (ADR 017 / RFC 006 Cap 2).

Implements:
ADR 017
RFC 006
Capability: Deposit Observation
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.wallet.exceptions import ObservationAttributionError
from apps.wallet.models import (
    DepositObservation,
    ObservationStatus,
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)
from apps.wallet.services import DepositObservationService, ObservationSignal

User = get_user_model()

_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_TX = "0x" + "11" * 32


def _wallet_address(*, email: str, index: int = 0) -> WalletAddress:
    user = User.objects.create_user(email=email, password="secret123")
    identity = WalletIdentity.objects.create(account=user.billing_account)
    return WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address=f"0x{index:040d}",
        derivation_index=index,
        status=WalletAddressStatus.ACTIVE,
    )


def _signal(
    *,
    to_address: str,
    confirmations: int = 25,
    log_index: int = 0,
    token: str = _USDT,
    amount: str = "10.000000",
) -> ObservationSignal:
    return ObservationSignal(
        chain=WalletChain.POLYGON,
        tx_hash=_TX,
        log_index=log_index,
        to_address=to_address,
        from_address="0x" + "22" * 20,
        amount=Decimal(amount),
        token_contract=token,
        confirmations=confirmations,
        block_number=12_345_678,
    )


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_ingest_confirms_when_depth_sufficient() -> None:
    addr = _wallet_address(email="obs-ok@example.com", index=0)
    svc = DepositObservationService()
    obs = svc.ingest(_signal(to_address=addr.address, confirmations=20))

    assert obs.status == ObservationStatus.CONFIRMED
    assert obs.confirmed_at is not None
    assert obs.tx_hash == _TX
    assert obs.log_index == 0


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_ingest_stays_pending_until_confirmations() -> None:
    addr = _wallet_address(email="obs-pend@example.com", index=1)
    svc = DepositObservationService()
    first = svc.ingest(_signal(to_address=addr.address, confirmations=5))
    assert first.status == ObservationStatus.PENDING_CONFIRMATION

    second = svc.ingest(_signal(to_address=addr.address, confirmations=21))
    assert second.pk == first.pk
    assert second.status == ObservationStatus.CONFIRMED


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_ingest_idempotent_same_identity() -> None:
    addr = _wallet_address(email="obs-idem@example.com", index=2)
    svc = DepositObservationService()
    a = svc.ingest(_signal(to_address=addr.address, confirmations=25))
    b = svc.ingest(_signal(to_address=addr.address, confirmations=30))
    assert a.pk == b.pk
    assert DepositObservation.objects.count() == 1
    assert b.status == ObservationStatus.CONFIRMED


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_same_tx_different_log_index_are_distinct() -> None:
    addr = _wallet_address(email="obs-logs@example.com", index=3)
    svc = DepositObservationService()
    a = svc.ingest(_signal(to_address=addr.address, log_index=0))
    b = svc.ingest(_signal(to_address=addr.address, log_index=1))
    assert a.pk != b.pk
    assert DepositObservation.objects.count() == 2


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_wrong_asset_rejected() -> None:
    addr = _wallet_address(email="obs-asset@example.com", index=4)
    svc = DepositObservationService()
    obs = svc.ingest(
        _signal(
            to_address=addr.address,
            token="0x0000000000000000000000000000000000000001",
        )
    )
    assert obs.status == ObservationStatus.REJECTED
    assert obs.status_reason == "unaccepted_asset"


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_unknown_address_raises() -> None:
    svc = DepositObservationService()
    with pytest.raises(ObservationAttributionError):
        svc.ingest(_signal(to_address="0x" + "99" * 20))


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_confirmed_does_not_mutate_account_balance() -> None:
    addr = _wallet_address(email="obs-bal@example.com", index=5)
    account = addr.wallet_identity.account
    before = account.balance
    DepositObservationService().ingest(
        _signal(to_address=addr.address, confirmations=50)
    )
    account.refresh_from_db()
    assert account.balance == before


@pytest.mark.django_db
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_expire_pending_observation() -> None:
    addr = _wallet_address(email="obs-exp@example.com", index=6)
    svc = DepositObservationService()
    obs = svc.ingest(_signal(to_address=addr.address, confirmations=1))
    assert obs.status == ObservationStatus.PENDING_CONFIRMATION
    expired = svc.expire(obs)
    assert expired.status == ObservationStatus.EXPIRED
    assert expired.expired_at is not None


@pytest.mark.django_db(transaction=True)
@override_settings(POLYGON_MIN_CONFIRMATIONS=20, POLYGON_USDT_CONTRACT=_USDT)
def test_concurrent_ingest_one_row() -> None:
    addr = _wallet_address(email="obs-conc@example.com", index=7)
    svc = DepositObservationService()
    signal = _signal(to_address=addr.address, confirmations=25)

    def _call() -> str:
        return str(svc.ingest(signal).pk)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_call) for _ in range(8)]
        ids = {f.result() for f in as_completed(futures)}

    assert len(ids) == 1
    assert DepositObservation.objects.count() == 1
