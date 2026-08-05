"""Polygon USDT BlockchainProvider implementation (ADR-010)."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from django.conf import settings

from apps.integrations.polygon.client import PolygonRpcClient
from shared.providers.blockchain import (
    BlockchainProviderError,
    BlockchainRPCError,
    TransferNotFoundError,
    TransferResult,
)

# keccak256("Transfer(address,address,uint256)")
_ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

_MONEY_QUANT = Decimal("0.000001")


class PolygonProvider:
    """Fetches USDT Transfer logs for a Polygon transaction hash."""

    def __init__(self, client: PolygonRpcClient | None = None) -> None:
        self.client = client or PolygonRpcClient()
        self.usdt_contract = _normalize_address(settings.POLYGON_USDT_CONTRACT)
        self.platform_wallet = _normalize_address(settings.POLYGON_PLATFORM_WALLET)
        self.chain_id = int(settings.POLYGON_CHAIN_ID)
        self.token_decimals = int(settings.POLYGON_USDT_DECIMALS)

        if not self.usdt_contract or self.usdt_contract == "0x":
            raise BlockchainProviderError("POLYGON_USDT_CONTRACT is not configured")
        if not self.platform_wallet or self.platform_wallet == "0x":
            raise BlockchainProviderError("POLYGON_PLATFORM_WALLET is not configured")

    def fetch_usdt_transfer(
        self, tx_hash: str, *, to_address: str | None = None
    ) -> TransferResult:
        """Return USDT transfer to ``to_address`` (or platform wallet).

        Matching rule (deterministic): first receipt log (in order) that is an
        ERC-20 ``Transfer`` from ``POLYGON_USDT_CONTRACT`` whose ``to`` equals
        the expected recipient. Other tokens and other recipients are skipped.
        Confirmations are reported only; min-confirm gating belongs to
        ``DepositVerificationService``.

        Raises:
            TransferNotFoundError: receipt missing or no matching Transfer log.
            BlockchainRPCError: RPC failed after retries.
            BlockchainProviderError: misconfiguration or unexpected chain.
        """
        normalized_hash = _normalize_tx_hash(tx_hash)
        self._assert_chain_id()
        expected_to = _normalize_address(to_address or self.platform_wallet)
        if not expected_to or expected_to == "0x":
            raise BlockchainProviderError("expected deposit to_address is empty")

        receipt = self.client.call("eth_getTransactionReceipt", [normalized_hash])
        if receipt is None:
            raise TransferNotFoundError(
                f"Transaction receipt not found for {normalized_hash}"
            )
        if not isinstance(receipt, dict):
            raise BlockchainRPCError("eth_getTransactionReceipt returned invalid type")

        head_hex = self.client.call("eth_blockNumber", [])
        head_block = _hex_to_int(head_hex)
        tx_block = _hex_to_int(receipt.get("blockNumber"))
        confirmations = max(head_block - tx_block + 1, 0)

        matched = self._find_usdt_transfer_log(
            receipt.get("logs") or [], expected_to=expected_to
        )
        if matched is None:
            raise TransferNotFoundError(
                f"No USDT transfer to expected wallet in {normalized_hash}"
            )

        from_address, to_addr, amount, log = matched
        tx_status = _receipt_status(receipt.get("status"))

        raw_rpc_response: dict[str, Any] = {
            "tx_hash": normalized_hash,
            "receipt": receipt,
            "block_number": head_hex,
            "chain_id": self.chain_id,
            "matched_log": log,
            "expected_to": expected_to,
        }

        return TransferResult(
            tx_hash=normalized_hash,
            from_address=from_address,
            to_address=to_addr,
            amount=amount,
            confirmations=confirmations,
            block_number=tx_block,
            token_contract=self.usdt_contract,
            status=tx_status,
            raw_rpc_response=raw_rpc_response,
        )

    def _assert_chain_id(self) -> None:
        remote_hex = self.client.call("eth_chainId", [])
        remote_id = _hex_to_int(remote_hex)
        if remote_id != self.chain_id:
            raise BlockchainProviderError(
                f"Unexpected chain_id {remote_id}; expected {self.chain_id}"
            )

    def _find_usdt_transfer_log(
        self, logs: list[Any], *, expected_to: str
    ) -> tuple[str, str, Decimal, dict[str, Any]] | None:
        """Return the first USDT→expected_to Transfer in receipt log order."""
        for log in logs:
            if not isinstance(log, dict):
                continue
            if _normalize_address(str(log.get("address") or "")) != self.usdt_contract:
                continue
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            if str(topics[0]).lower() != _ERC20_TRANSFER_TOPIC:
                continue
            to_address = _topic_to_address(str(topics[2]))
            if to_address != expected_to:
                continue
            from_address = _topic_to_address(str(topics[1]))
            amount = self._amount_from_data(str(log.get("data") or "0x0"))
            return from_address, to_address, amount, log
        return None

    def _amount_from_data(self, data: str) -> Decimal:
        raw = _hex_to_int(data)
        scale = Decimal(10) ** self.token_decimals
        amount = Decimal(raw) / scale
        return amount.quantize(_MONEY_QUANT, rounding=ROUND_DOWN)


def _normalize_address(value: str) -> str:
    text = (value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return "0x"
    return "0x" + text.zfill(40)[-40:]


def _normalize_tx_hash(value: str) -> str:
    text = (value or "").strip().lower()
    if not text.startswith("0x"):
        text = "0x" + text
    if len(text) != 66:
        raise TransferNotFoundError(f"Invalid transaction hash: {value!r}")
    try:
        int(text, 16)
    except ValueError as exc:
        raise TransferNotFoundError(f"Invalid transaction hash: {value!r}") from exc
    return text


def _topic_to_address(topic: str) -> str:
    text = topic.lower()
    if text.startswith("0x"):
        text = text[2:]
    return "0x" + text[-40:]


def _hex_to_int(value: Any) -> int:
    if value is None:
        raise BlockchainRPCError("missing hex integer in RPC response")
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16)
    return int(text, 10)


def _receipt_status(value: Any) -> str:
    if value is None:
        return "unknown"
    status = _hex_to_int(value)
    if status == 1:
        return "success"
    if status == 0:
        return "reverted"
    return "unknown"
