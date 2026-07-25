"""Build deposit-info payload for the billing API (ADR-010)."""

from __future__ import annotations

from typing import Any

from django.conf import settings

TOKEN_SYMBOL = "USDT"


def build_eip681_uri(
    *,
    wallet: str,
    contract: str,
    chain_id: int,
) -> str:
    """Amount-agnostic EIP-681 ERC-20 transfer URI.

    Clients append ``&uint256=<base_units>`` when the deposit amount is known.
    Format: ``ethereum:<token>@<chain_id>/transfer?address=<recipient>``
    """
    token = (contract or "").strip()
    recipient = (wallet or "").strip()
    return f"ethereum:{token}@{chain_id}/transfer?address={recipient}"


def get_deposit_info() -> dict[str, Any]:
    """Return full token/chain meta so frontends never hardcode Polygon USDT."""
    wallet = (settings.POLYGON_PLATFORM_WALLET or "").strip()
    contract = (settings.POLYGON_USDT_CONTRACT or "").strip()
    chain_id = int(settings.POLYGON_CHAIN_ID)
    return {
        "wallet": wallet,
        "chain_id": chain_id,
        "token_symbol": TOKEN_SYMBOL,
        "token_decimals": int(settings.POLYGON_USDT_DECIMALS),
        "contract": contract,
        "min_confirmations": int(settings.POLYGON_MIN_CONFIRMATIONS),
        "eip681_uri": build_eip681_uri(
            wallet=wallet, contract=contract, chain_id=chain_id
        ),
        "walletconnect_enabled": bool(settings.WALLETCONNECT_ENABLED),
        "subscriptions_enabled": bool(settings.SUBSCRIPTIONS_ENABLED),
    }
