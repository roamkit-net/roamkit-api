"""Build deposit-info and billing display-config payloads (ADR-010 / ADR 018)."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from django.conf import settings

from apps.billing.models import Account
from apps.wallet.services.allocation import WalletAllocationService
from apps.wallet.services.flags import should_expose_wallet_address

# Shared display/token constants — deposit-info and billing/config must not drift.
CONFIG_VERSION = 1
TOKEN_SYMBOL = "USDT"
TOKEN_NAME = "USDT Credits"
DISPLAY_DECIMALS = 2
BILLING_CONFIG_CACHE_MAX_AGE = 300


def token_decimals() -> int:
    """Ledger / on-chain token precision (``Decimal(20,6)`` / POLYGON_USDT_DECIMALS)."""
    return int(settings.POLYGON_USDT_DECIMALS)


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


def get_billing_config() -> dict[str, Any]:
    """Return public display configuration (no wallet/contract/EIP-681 secrets)."""
    return {
        "config_version": CONFIG_VERSION,
        "token_symbol": TOKEN_SYMBOL,
        "token_name": TOKEN_NAME,
        "token_decimals": token_decimals(),
        "display_decimals": DISPLAY_DECIMALS,
        "billing_enabled": bool(settings.BILLING_ENABLED),
    }


def billing_config_etag(payload: dict[str, Any]) -> str:
    """Strong ETag from a stable JSON encoding of the config payload."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def resolve_deposit_wallet(*, account: Account | None = None) -> str:
    """Shared ADR 010 wallet, or cohort WalletAddress (Limited Traffic)."""
    if account is not None and should_expose_wallet_address(account.pk):
        addr = WalletAllocationService().ensure_active_address(account)
        return (addr.address or "").strip()
    return (settings.POLYGON_PLATFORM_WALLET or "").strip()


def get_deposit_info(*, account: Account | None = None) -> dict[str, Any]:
    """Return full token/chain meta so frontends never hardcode Polygon USDT.

    When ``account`` is in the cutover cohort and ``WALLET_ADDRESS_ENABLED``,
    ``wallet`` is the Account's active WalletAddress. Otherwise the shared
    ADR 010 platform wallet. Empty cohort ⇒ always shared (instant rollback).
    """
    wallet = resolve_deposit_wallet(account=account)
    contract = (settings.POLYGON_USDT_CONTRACT or "").strip()
    chain_id = int(settings.POLYGON_CHAIN_ID)
    return {
        "wallet": wallet,
        "chain_id": chain_id,
        "token_symbol": TOKEN_SYMBOL,
        "token_decimals": token_decimals(),
        "contract": contract,
        "min_confirmations": int(settings.POLYGON_MIN_CONFIRMATIONS),
        "eip681_uri": build_eip681_uri(
            wallet=wallet, contract=contract, chain_id=chain_id
        ),
        "walletconnect_enabled": bool(settings.WALLETCONNECT_ENABLED),
        "subscriptions_enabled": bool(settings.SUBSCRIPTIONS_ENABLED),
        "vouchers_enabled": bool(settings.VOUCHERS_ENABLED),
    }


def deposit_info_for_account_id(account_id: UUID | str) -> dict[str, Any]:
    """Helper for callers that only have an Account primary key."""
    account = Account.objects.filter(pk=account_id).first()
    return get_deposit_info(account=account)
