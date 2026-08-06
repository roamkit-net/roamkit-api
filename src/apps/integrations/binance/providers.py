"""Binance Funding Provider adapter (ADR 017 / RFC 005).

Guides USDT withdrawals to a RoamKit ``WalletAddress``. Never mutates Credits.
Provider history is not Credits SoT — Observation + Confirmation is.
"""

from __future__ import annotations

from django.conf import settings

from apps.wallet.models import WalletAddress, WalletAddressStatus, WalletChain
from apps.wallet.services.hd import normalize_evm_address
from shared.providers.funding import (
    FundingDepositGuide,
    FundingDepositRequest,
    FundingProviderError,
    FundingProviderMetadata,
    FundingProviderUnsupportedError,
    FundingStatus,
)

PROVIDER_ID = "binance"
_ASSET_USDT = "USDT"
_FUNDING_SOURCE = "exchange_withdrawal"

# Binance UI network label for Polygon PoS (ops catalog; not Wallet domain).
_DEFAULT_NETWORK_LABEL = "MATIC"


class BinanceFundingProvider:
    """Binance adapter: present destination + withdraw guide / status for UX."""

    def metadata(self) -> FundingProviderMetadata:
        label = getattr(settings, "BINANCE_POLYGON_NETWORK_LABEL", "") or ""
        network = label or _DEFAULT_NETWORK_LABEL
        return FundingProviderMetadata(
            provider_id=PROVIDER_ID,
            display_name="Binance",
            assets=(_ASSET_USDT,),
            chains=(WalletChain.POLYGON,),
            network_labels={WalletChain.POLYGON: network},
            capabilities=("deposit", "status", "metadata"),
            limits_note=(
                "Withdraw USDT on the Polygon (MATIC) network to the RoamKit "
                "destination. Credits are granted only after on-chain confirmation."
            ),
            extra={
                "withdraw_url": getattr(settings, "BINANCE_WITHDRAW_URL", "") or "",
            },
        )

    def deposit(self, request: FundingDepositRequest) -> FundingDepositGuide:
        self._validate_request(request)
        wallet = self._require_active_wallet_address(request)
        meta = self.metadata()
        network = meta.network_labels[request.chain]
        destination = normalize_evm_address(request.destination_address)
        if destination != wallet.address:
            raise FundingProviderError(
                "destination_address does not match WalletAddress record"
            )

        session_id = self._session_id(
            wallet_address_id=str(wallet.pk),
            chain=request.chain,
            asset=request.asset.upper(),
        )
        withdraw_url = (meta.extra.get("withdraw_url") or "").strip() or None
        instructions = (
            f"On Binance, withdraw {_ASSET_USDT} via network {network} to "
            f"{destination}. Do not use other networks. Credits appear only after "
            f"RoamKit confirms the on-chain transfer (not when Binance marks "
            f"withdraw complete)."
        )
        return FundingDepositGuide(
            provider_id=PROVIDER_ID,
            funding_source=_FUNDING_SOURCE,
            session_id=session_id,
            wallet_address_id=wallet.pk,
            destination_address=destination,
            chain=request.chain,
            asset=_ASSET_USDT,
            network_label=network,
            instructions=instructions,
            provider_url=withdraw_url,
        )

    def status(self, session_id: str) -> FundingStatus:
        wallet_address_id, chain, asset = self._parse_session_id(session_id)
        wallet = WalletAddress.objects.filter(pk=wallet_address_id).first()
        if wallet is None:
            return FundingStatus(
                provider_id=PROVIDER_ID,
                session_id=session_id,
                state="invalid",
                message="Unknown funding session (WalletAddress not found).",
            )
        return FundingStatus(
            provider_id=PROVIDER_ID,
            session_id=session_id,
            state="awaiting_deposit",
            message=(
                "Waiting for USDT on Polygon at the RoamKit destination. "
                "Binance withdraw status is UX-only; Credits require on-chain "
                "Confirmation."
            ),
            destination_address=wallet.address,
            chain=chain,
            asset=asset,
        )

    def _validate_request(self, request: FundingDepositRequest) -> None:
        asset = (request.asset or "").upper()
        if asset != _ASSET_USDT:
            raise FundingProviderUnsupportedError(
                f"Binance adapter supports {_ASSET_USDT} only, got {request.asset!r}"
            )
        if request.chain != WalletChain.POLYGON:
            raise FundingProviderUnsupportedError(
                f"Binance adapter supports polygon only, got {request.chain!r}"
            )

    def _require_active_wallet_address(
        self, request: FundingDepositRequest
    ) -> WalletAddress:
        wallet = WalletAddress.objects.filter(pk=request.wallet_address_id).first()
        if wallet is None:
            raise FundingProviderError(
                f"WalletAddress {request.wallet_address_id} not found"
            )
        if wallet.chain != request.chain:
            raise FundingProviderError("WalletAddress chain mismatch")
        if wallet.status != WalletAddressStatus.ACTIVE:
            raise FundingProviderError(
                "destination must be an active WalletAddress (RFC 005)"
            )
        return wallet

    @staticmethod
    def _session_id(*, wallet_address_id: str, chain: str, asset: str) -> str:
        return f"binance:v1:{wallet_address_id}:{chain}:{asset}"

    @staticmethod
    def _parse_session_id(session_id: str) -> tuple[str, str, str]:
        parts = (session_id or "").split(":")
        if len(parts) != 5 or parts[0] != "binance" or parts[1] != "v1":
            raise FundingProviderError(f"invalid Binance session_id: {session_id!r}")
        return parts[2], parts[3], parts[4]
