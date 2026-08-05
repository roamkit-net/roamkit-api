"""Start Funding Provider flows against an active WalletAddress (RFC 005)."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from apps.billing.models import Account
from apps.wallet.models import WalletChain
from apps.wallet.services.allocation import WalletAllocationService
from shared.providers.funding import (
    FundingDepositGuide,
    FundingDepositRequest,
    FundingProvider,
    FundingProviderError,
    FundingProviderMetadata,
    FundingStatus,
)

_PROVIDER_SETTINGS = {
    "mexc": "MEXC_FUNDING_PROVIDER",
    "binance": "BINANCE_FUNDING_PROVIDER",
}


class FundingService:
    """Destination-contract glue: allocate active address, then provider.deposit.

    Never calls CreditService. Provider status is UX/ops only.
    """

    def __init__(
        self,
        *,
        allocation: WalletAllocationService | None = None,
    ) -> None:
        self.allocation = allocation or WalletAllocationService()

    def metadata(self, provider_id: str) -> FundingProviderMetadata:
        return self._provider(provider_id).metadata()

    def start_deposit(
        self,
        account: Account,
        *,
        provider_id: str = "mexc",
        chain: str = WalletChain.POLYGON,
        asset: str = "USDT",
    ) -> FundingDepositGuide:
        address = self.allocation.ensure_active_address(account, chain=chain)
        request = FundingDepositRequest(
            wallet_address_id=address.pk,
            destination_address=address.address,
            chain=chain,
            asset=asset,
        )
        return self._provider(provider_id).deposit(request)

    def status(self, provider_id: str, session_id: str) -> FundingStatus:
        return self._provider(provider_id).status(session_id)

    def _provider(self, provider_id: str) -> FundingProvider:
        key = (provider_id or "").strip().lower()
        setting_name = _PROVIDER_SETTINGS.get(key)
        if not setting_name:
            raise FundingProviderError(f"unknown funding provider: {provider_id!r}")
        path = getattr(settings, setting_name, "") or ""
        if not path:
            raise FundingProviderError(f"{setting_name} is not configured")
        cls = import_string(path)
        return cls()
