"""WalletAddress allocation service tests (ADR 017 / RFC 004)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.wallet.exceptions import (
    WalletAddressNotFoundError,
    WalletSeedNotConfiguredError,
)
from apps.wallet.models import WalletAddress, WalletAddressStatus, WalletIdentity
from apps.wallet.services import WalletAllocationService
from apps.wallet.services.hd import derive_evm_address

User = get_user_model()

# Well-known Hardhat / Anvil mnemonic — test-only, not a production seed.
_TEST_MNEMONIC = "test test test test test test test test test test test junk"
_ADDR_0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
_ADDR_1 = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_ensure_active_address_allocates_index_zero() -> None:
    user = User.objects.create_user(email="alloc-0@example.com", password="secret123")
    svc = WalletAllocationService()
    addr = svc.ensure_active_address(user.billing_account)

    assert addr.derivation_index == 0
    assert addr.address == _ADDR_0
    assert addr.status == WalletAddressStatus.ACTIVE
    assert addr.chain == "polygon"
    assert WalletIdentity.objects.filter(account=user.billing_account).count() == 1


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_ensure_active_address_is_idempotent() -> None:
    user = User.objects.create_user(
        email="alloc-idem@example.com", password="secret123"
    )
    svc = WalletAllocationService()
    first = svc.ensure_active_address(user.billing_account)
    second = svc.ensure_active_address(user.billing_account)

    assert first.pk == second.pk
    assert (
        WalletAddress.objects.filter(wallet_identity=first.wallet_identity).count() == 1
    )


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_second_account_gets_next_index_never_reuse() -> None:
    u1 = User.objects.create_user(email="a1@example.com", password="secret123")
    u2 = User.objects.create_user(email="a2@example.com", password="secret123")
    svc = WalletAllocationService()
    a1 = svc.ensure_active_address(u1.billing_account)
    a2 = svc.ensure_active_address(u2.billing_account)

    assert a1.derivation_index == 0
    assert a2.derivation_index == 1
    assert a1.address == _ADDR_0
    assert a2.address == _ADDR_1
    assert a1.address != a2.address


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_rotate_retires_and_allocates_new_index() -> None:
    user = User.objects.create_user(email="rotate@example.com", password="secret123")
    svc = WalletAllocationService()
    first = svc.ensure_active_address(user.billing_account)
    second = svc.rotate_active_address(user.billing_account)

    first.refresh_from_db()
    assert first.status == WalletAddressStatus.RETIRED
    assert first.retired_at is not None
    assert second.status == WalletAddressStatus.ACTIVE
    assert second.derivation_index == 1
    assert second.pk != first.pk
    assert (
        WalletAddress.objects.filter(wallet_identity=first.wallet_identity).count() == 2
    )


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_rotate_without_active_raises() -> None:
    user = User.objects.create_user(
        email="rotate-none@example.com", password="secret123"
    )
    with pytest.raises(WalletAddressNotFoundError):
        WalletAllocationService().rotate_active_address(user.billing_account)


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC="")
def test_allocate_requires_mnemonic() -> None:
    user = User.objects.create_user(email="no-seed@example.com", password="secret123")
    with pytest.raises(WalletSeedNotConfiguredError):
        WalletAllocationService().ensure_active_address(user.billing_account)


def test_derive_matches_lab_poc_vectors() -> None:
    assert derive_evm_address(mnemonic=_TEST_MNEMONIC, derivation_index=0) == _ADDR_0
    assert derive_evm_address(mnemonic=_TEST_MNEMONIC, derivation_index=1) == _ADDR_1


@pytest.mark.django_db(transaction=True)
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_concurrent_ensure_converges_on_one_active() -> None:
    user = User.objects.create_user(
        email="concurrent@example.com", password="secret123"
    )
    account = user.billing_account
    svc = WalletAllocationService()

    def _call() -> str:
        return str(svc.ensure_active_address(account).pk)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_call) for _ in range(8)]
        ids = {f.result() for f in as_completed(futures)}

    assert len(ids) == 1
    assert (
        WalletAddress.objects.filter(
            wallet_identity__account=account,
            status=WalletAddressStatus.ACTIVE,
        ).count()
        == 1
    )
    assert WalletAddress.objects.filter(wallet_identity__account=account).count() == 1


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_allocation_does_not_change_account_balance() -> None:
    user = User.objects.create_user(email="bal@example.com", password="secret123")
    account = user.billing_account
    before = account.balance
    WalletAllocationService().ensure_active_address(account)
    account.refresh_from_db()
    assert account.balance == before
