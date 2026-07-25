"""Blockchain provider protocol and domain DTOs (ADR-010)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class BlockchainProviderError(Exception):
    """Base error for blockchain provider operations."""


class BlockchainRPCError(BlockchainProviderError):
    """Raised when RPC calls fail after timeout/retry exhaustion."""


class TransferNotFoundError(BlockchainProviderError):
    """Raised when the tx is missing or has no matching USDT transfer."""


@dataclass(frozen=True)
class TransferResult:
    """Normalized USDT transfer observed on-chain for a deposit tx."""

    tx_hash: str
    from_address: str
    to_address: str
    amount: Decimal
    confirmations: int
    block_number: int
    token_contract: str
    status: str
    raw_rpc_response: dict[str, Any]


class BlockchainProvider(Protocol):
    """Fetches on-chain USDT transfers for deposit verification."""

    def fetch_usdt_transfer(self, tx_hash: str) -> TransferResult: ...
