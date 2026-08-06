"""Idempotent WalletAddress allocation (ADR 017 / RFC 004)."""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.billing.models import Account
from apps.wallet.exceptions import (
    WalletAddressNotFoundError,
    WalletAllocationError,
)
from apps.wallet.models import (
    WalletAddress,
    WalletAddressStatus,
    WalletChain,
    WalletIdentity,
)
from apps.wallet.services.hd import derive_evm_address

_MAX_INDEX_RETRIES = 8


class WalletAllocationService:
    """Allocate and rotate receive addresses for a billing Account.

    Index Registry rows are ``WalletAddress`` records. Indices are reserved on
    persist and never reused. Concurrent ensure calls for the same Account +
    Chain converge on one active address.
    """

    def ensure_active_address(
        self,
        account: Account,
        *,
        chain: str = WalletChain.POLYGON,
    ) -> WalletAddress:
        """Return the active ``WalletAddress`` for ``account`` + ``chain``.

        Creates ``WalletIdentity`` and allocates a new Index Registry row on
        first call. Idempotent: repeats return the same active row.
        """
        for _ in range(_MAX_INDEX_RETRIES):
            try:
                return self._ensure_active_address_once(account, chain=chain)
            except IntegrityError:
                continue
        raise WalletAllocationError(
            "failed to allocate WalletAddress after concurrency retries"
        )

    def rotate_active_address(
        self,
        account: Account,
        *,
        chain: str = WalletChain.POLYGON,
    ) -> WalletAddress:
        """Retire the active address and allocate a new unused index.

        Retired indices remain in the registry (never reused). Late-deposit
        watch of retired addresses is out of scope for Cap 1.
        """
        for _ in range(_MAX_INDEX_RETRIES):
            try:
                return self._rotate_active_address_once(account, chain=chain)
            except IntegrityError:
                continue
        raise WalletAllocationError(
            "failed to rotate WalletAddress after concurrency retries"
        )

    def _ensure_active_address_once(
        self,
        account: Account,
        *,
        chain: str,
    ) -> WalletAddress:
        with transaction.atomic():
            identity = self._lock_identity(account)
            existing = (
                WalletAddress.objects.filter(
                    wallet_identity=identity,
                    chain=chain,
                    status=WalletAddressStatus.ACTIVE,
                )
                .order_by("derivation_index")
                .first()
            )
            if existing is not None:
                return existing
            return self._allocate_new(identity, chain=chain)

    def _rotate_active_address_once(
        self,
        account: Account,
        *,
        chain: str,
    ) -> WalletAddress:
        with transaction.atomic():
            identity = self._lock_identity(account)
            active = (
                WalletAddress.objects.select_for_update()
                .filter(
                    wallet_identity=identity,
                    chain=chain,
                    status=WalletAddressStatus.ACTIVE,
                )
                .first()
            )
            if active is None:
                raise WalletAddressNotFoundError(
                    f"no active WalletAddress for account={account.pk} chain={chain}"
                )
            active.status = WalletAddressStatus.RETIRED
            active.retired_at = timezone.now()
            active.save(update_fields=["status", "retired_at"])
            return self._allocate_new(identity, chain=chain)

    def _lock_identity(self, account: Account) -> WalletIdentity:
        identity = WalletIdentity.objects.filter(account=account).first()
        if identity is None:
            try:
                with transaction.atomic():
                    identity = WalletIdentity.objects.create(account=account)
            except IntegrityError:
                identity = WalletIdentity.objects.get(account=account)
        return WalletIdentity.objects.select_for_update().get(pk=identity.pk)

    def _allocate_new(
        self,
        identity: WalletIdentity,
        *,
        chain: str,
    ) -> WalletAddress:
        derivation_index = self._next_derivation_index()
        mnemonic = getattr(settings, "WALLET_HD_MNEMONIC", "") or ""
        address = derive_evm_address(
            mnemonic=mnemonic,
            derivation_index=derivation_index,
        )
        return WalletAddress.objects.create(
            wallet_identity=identity,
            chain=chain,
            address=address,
            derivation_index=derivation_index,
            status=WalletAddressStatus.ACTIVE,
        )

    @staticmethod
    def _next_derivation_index() -> int:
        current = WalletAddress.objects.aggregate(m=Max("derivation_index"))["m"]
        return 0 if current is None else int(current) + 1
