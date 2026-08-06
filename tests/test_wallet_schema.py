"""Model tests for apps.wallet schema (Capability: Wallet Address Allocation).

Implements:
ADR 017
RFC 004
Capability: Wallet Address Allocation (schema)
"""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.wallet.models import (
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)


@pytest.mark.django_db
def test_wallet_identity_one_to_one_with_account() -> None:
    user = User.objects.create_user(email="wallet-id@example.com", password="secret123")
    account = user.billing_account
    identity = WalletIdentity.objects.create(account=account)
    assert isinstance(identity.pk, uuid.UUID)
    assert account.wallet_identity.pk == identity.pk

    with pytest.raises(IntegrityError), transaction.atomic():
        WalletIdentity.objects.create(account=account)


@pytest.mark.django_db
def test_wallet_address_uuid_pk_and_index_registry_fields() -> None:
    user = User.objects.create_user(
        email="wallet-addr@example.com", password="secret123"
    )
    identity = WalletIdentity.objects.create(account=user.billing_account)
    addr = WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address="0x1111111111111111111111111111111111111111",
        derivation_index=0,
        status=WalletAddressStatus.ACTIVE,
    )
    assert isinstance(addr.pk, uuid.UUID)
    assert addr.derivation_index == 0
    assert addr.retired_at is None


@pytest.mark.django_db
def test_derivation_index_never_reused_globally() -> None:
    user_a = User.objects.create_user(email="idx-a@example.com", password="secret123")
    user_b = User.objects.create_user(email="idx-b@example.com", password="secret123")
    id_a = WalletIdentity.objects.create(account=user_a.billing_account)
    id_b = WalletIdentity.objects.create(account=user_b.billing_account)

    WalletAddress.objects.create(
        wallet_identity=id_a,
        chain=WalletChain.POLYGON,
        address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        derivation_index=7,
        status=WalletAddressStatus.ACTIVE,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        WalletAddress.objects.create(
            wallet_identity=id_b,
            chain=WalletChain.POLYGON,
            address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            derivation_index=7,
            status=WalletAddressStatus.ACTIVE,
        )


@pytest.mark.django_db
def test_at_most_one_active_address_per_identity_chain() -> None:
    user = User.objects.create_user(
        email="one-active@example.com", password="secret123"
    )
    identity = WalletIdentity.objects.create(account=user.billing_account)
    WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address="0xcccccccccccccccccccccccccccccccccccccccc",
        derivation_index=0,
        status=WalletAddressStatus.ACTIVE,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        WalletAddress.objects.create(
            wallet_identity=identity,
            chain=WalletChain.POLYGON,
            address="0xdddddddddddddddddddddddddddddddddddddddd",
            derivation_index=1,
            status=WalletAddressStatus.ACTIVE,
        )


@pytest.mark.django_db
def test_retired_allows_new_active_with_new_index() -> None:
    user = User.objects.create_user(email="rotate@example.com", password="secret123")
    identity = WalletIdentity.objects.create(account=user.billing_account)
    old = WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        derivation_index=0,
        status=WalletAddressStatus.ACTIVE,
    )
    old.status = WalletAddressStatus.RETIRED
    old.retired_at = timezone.now()
    old.save(update_fields=["status", "retired_at"])

    new = WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address="0xffffffffffffffffffffffffffffffffffffffff",
        derivation_index=1,
        status=WalletAddressStatus.ACTIVE,
    )
    assert new.status == WalletAddressStatus.ACTIVE
    assert (
        WalletAddress.objects.filter(
            wallet_identity=identity,
            chain=WalletChain.POLYGON,
            status=WalletAddressStatus.ACTIVE,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_retired_must_set_retired_at() -> None:
    user = User.objects.create_user(
        email="retire-check@example.com", password="secret123"
    )
    identity = WalletIdentity.objects.create(account=user.billing_account)
    with pytest.raises(IntegrityError), transaction.atomic():
        WalletAddress.objects.create(
            wallet_identity=identity,
            chain=WalletChain.POLYGON,
            address="0x1212121212121212121212121212121212121212",
            derivation_index=0,
            status=WalletAddressStatus.RETIRED,
            retired_at=None,
        )


@pytest.mark.django_db
def test_wallet_models_do_not_touch_account_balance() -> None:
    user = User.objects.create_user(email="no-credit@example.com", password="secret123")
    account = user.billing_account
    before = account.balance
    identity = WalletIdentity.objects.create(account=account)
    WalletAddress.objects.create(
        wallet_identity=identity,
        chain=WalletChain.POLYGON,
        address="0x3434343434343434343434343434343434343434",
        derivation_index=0,
        status=WalletAddressStatus.ACTIVE,
    )
    account.refresh_from_db()
    assert account.balance == before
