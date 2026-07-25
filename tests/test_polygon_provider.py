"""Tests for Polygon BlockchainProvider (RPC client + USDT transfer fetch)."""

from __future__ import annotations

import io
import urllib.error
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.integrations.polygon.client import PolygonRpcClient
from apps.integrations.polygon.providers import PolygonProvider
from shared.providers.blockchain import (
    BlockchainProviderError,
    BlockchainRPCError,
    TransferNotFoundError,
    TransferResult,
)
from shared.providers.factory import get_blockchain_provider

USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
PLATFORM = "0x1111111111111111111111111111111111111111"
SENDER = "0x2222222222222222222222222222222222222222"
TX_HASH = "0x" + "ab" * 32
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_SETTINGS = {
    "POLYGON_RPC_URL": "https://rpc.example",
    "POLYGON_USDT_CONTRACT": USDT,
    "POLYGON_PLATFORM_WALLET": PLATFORM,
    "POLYGON_CHAIN_ID": 137,
    "POLYGON_USDT_DECIMALS": 6,
    "POLYGON_RPC_TIMEOUT": 10,
    "POLYGON_RPC_RETRIES": 3,
    "POLYGON_RPC_BACKOFF_BASE": 0.01,
}


def _pad_address(address: str) -> str:
    return "0x" + address[2:].lower().zfill(64)


def _transfer_log(
    *,
    to_address: str = PLATFORM,
    from_address: str = SENDER,
    amount_raw: int = 15_500_000,
    contract: str = USDT,
) -> dict[str, Any]:
    return {
        "address": contract,
        "topics": [
            TRANSFER_TOPIC,
            _pad_address(from_address),
            _pad_address(to_address),
        ],
        "data": hex(amount_raw),
    }


def _receipt(*, logs: list[dict[str, Any]] | None = None, status: str = "0x1") -> dict:
    return {
        "status": status,
        "blockNumber": "0x100",
        "transactionHash": TX_HASH,
        "logs": logs if logs is not None else [_transfer_log()],
    }


class _ScriptedRpcClient:
    """Deterministic RPC stub keyed by method name."""

    def __init__(
        self,
        results: dict[str, Any] | None = None,
        *,
        errors: dict[str, list[Exception]] | None = None,
    ) -> None:
        self.results = results or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, list[Any] | None]] = []

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self.calls.append((method, params))
        pending = self.errors.get(method) or []
        if pending:
            exc = pending.pop(0)
            raise exc
        if method not in self.results:
            raise AssertionError(f"unexpected RPC method {method}")
        return self.results[method]


@pytest.fixture
def provider_settings():
    with override_settings(**_SETTINGS):
        yield


@pytest.mark.django_db
def test_fetch_usdt_transfer_success(provider_settings) -> None:
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(),
            "eth_blockNumber": "0x113",  # 275 decimal; confirmations = 20
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    result = provider.fetch_usdt_transfer(TX_HASH)

    assert isinstance(result, TransferResult)
    assert result.tx_hash == TX_HASH
    assert result.from_address == SENDER
    assert result.to_address == PLATFORM
    assert result.amount == Decimal("15.500000")
    assert result.confirmations == 20
    assert result.block_number == 256
    assert result.token_contract == USDT
    assert result.status == "success"
    assert result.raw_rpc_response["matched_log"]["data"] == hex(15_500_000)
    assert result.raw_rpc_response["chain_id"] == 137


@pytest.mark.django_db
def test_fetch_usdt_transfer_ignores_other_recipients(provider_settings) -> None:
    logs = [
        _transfer_log(to_address="0x3333333333333333333333333333333333333333"),
        _transfer_log(amount_raw=1_000_000),
    ]
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(logs=logs),
            "eth_blockNumber": "0x100",
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    result = provider.fetch_usdt_transfer(TX_HASH)

    assert result.amount == Decimal("1.000000")
    assert result.to_address == PLATFORM


@pytest.mark.django_db
def test_fetch_usdt_transfer_ignores_other_erc20_contracts(provider_settings) -> None:
    other_token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    logs = [
        _transfer_log(contract=other_token, amount_raw=99_000_000),
        _transfer_log(amount_raw=2_000_000),
    ]
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(logs=logs),
            "eth_blockNumber": "0x100",
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    result = provider.fetch_usdt_transfer(TX_HASH)

    assert result.amount == Decimal("2.000000")
    assert result.token_contract == USDT


@pytest.mark.django_db
def test_fetch_usdt_transfer_picks_first_matching_log(provider_settings) -> None:
    """When several USDT→platform Transfers exist, first log in order wins."""
    logs = [
        _transfer_log(amount_raw=1_000_000),
        _transfer_log(amount_raw=9_000_000),
    ]
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(logs=logs),
            "eth_blockNumber": "0x100",
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    result = provider.fetch_usdt_transfer(TX_HASH)

    assert result.amount == Decimal("1.000000")
    assert result.raw_rpc_response["matched_log"]["data"] == hex(1_000_000)


@pytest.mark.django_db
def test_fetch_usdt_transfer_missing_receipt(provider_settings) -> None:
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": None,
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(TransferNotFoundError, match="receipt not found"):
        provider.fetch_usdt_transfer(TX_HASH)


@pytest.mark.django_db
def test_fetch_usdt_transfer_no_matching_log(provider_settings) -> None:
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(logs=[]),
            "eth_blockNumber": "0x100",
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(TransferNotFoundError, match="No USDT transfer"):
        provider.fetch_usdt_transfer(TX_HASH)


@pytest.mark.django_db
def test_fetch_usdt_transfer_rejects_wrong_chain_id(provider_settings) -> None:
    client = _ScriptedRpcClient({"eth_chainId": "0x1"})
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(BlockchainProviderError, match="Unexpected chain_id"):
        provider.fetch_usdt_transfer(TX_HASH)


@pytest.mark.django_db
def test_fetch_usdt_transfer_invalid_hash(provider_settings) -> None:
    provider = PolygonProvider(client=_ScriptedRpcClient())  # type: ignore[arg-type]

    with pytest.raises(TransferNotFoundError, match="Invalid transaction hash"):
        provider.fetch_usdt_transfer("0x1234")


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _rpc_client() -> PolygonRpcClient:
    return PolygonRpcClient(
        rpc_url="https://rpc.example",
        timeout=10,
        retries=3,
        backoff_base=0.01,
    )


@pytest.mark.django_db
def test_rpc_client_retries_then_succeeds(provider_settings) -> None:
    responses = [
        TimeoutError("slow"),
        TimeoutError("still slow"),
        b'{"jsonrpc":"2.0","id":1,"result":"0x89"}',
    ]

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeHttpResponse(item)

    client = _rpc_client()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with patch("apps.integrations.polygon.client.time.sleep") as sleep:
            assert client.call("eth_chainId", []) == "0x89"
            assert sleep.call_count == 2


@pytest.mark.django_db
def test_rpc_client_exhausts_retries_on_timeout(provider_settings) -> None:
    def always_timeout(request, timeout=None):  # noqa: ANN001
        raise TimeoutError("nope")

    client = _rpc_client()
    with patch("urllib.request.urlopen", side_effect=always_timeout):
        with patch("apps.integrations.polygon.client.time.sleep") as sleep:
            with pytest.raises(BlockchainRPCError, match="failed after 4 attempts"):
                client.call("eth_blockNumber", [])
            assert sleep.call_count == 3


@pytest.mark.django_db
def test_rpc_client_http_500_is_retried_then_fails(provider_settings) -> None:
    def always_500(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url="https://rpc.example",
            code=500,
            msg="Internal Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"boom"),
        )

    client = _rpc_client()
    with patch("urllib.request.urlopen", side_effect=always_500):
        with patch("apps.integrations.polygon.client.time.sleep") as sleep:
            with pytest.raises(BlockchainRPCError, match="failed after 4 attempts"):
                client.call("eth_blockNumber", [])
            assert sleep.call_count == 3


@pytest.mark.django_db
def test_rpc_client_malformed_json_is_retried_then_fails(provider_settings) -> None:
    def always_bad_json(request, timeout=None):  # noqa: ANN001
        return _FakeHttpResponse(b"not-json{")

    client = _rpc_client()
    with patch("urllib.request.urlopen", side_effect=always_bad_json):
        with patch("apps.integrations.polygon.client.time.sleep") as sleep:
            with pytest.raises(BlockchainRPCError, match="failed after 4 attempts"):
                client.call("eth_chainId", [])
            assert sleep.call_count == 3


@pytest.mark.django_db
def test_rpc_client_jsonrpc_error_fails_without_retry(provider_settings) -> None:
    payload = (
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"method not found"}}'
    )

    def once(request, timeout=None):  # noqa: ANN001
        return _FakeHttpResponse(payload)

    client = _rpc_client()
    with patch("urllib.request.urlopen", side_effect=once) as urlopen:
        with patch("apps.integrations.polygon.client.time.sleep") as sleep:
            with pytest.raises(
                BlockchainRPCError, match="JSON-RPC error: method not found"
            ):
                client.call("eth_chainId", [])
            assert urlopen.call_count == 1
            assert sleep.call_count == 0


@pytest.mark.django_db
def test_factory_returns_polygon_provider(provider_settings) -> None:
    with override_settings(
        **_SETTINGS,
        BLOCKCHAIN_PROVIDER="apps.integrations.polygon.providers.PolygonProvider",
    ):
        provider = get_blockchain_provider()
        assert isinstance(provider, PolygonProvider)


@pytest.mark.django_db
def test_reverted_receipt_status(provider_settings) -> None:
    client = _ScriptedRpcClient(
        {
            "eth_chainId": "0x89",
            "eth_getTransactionReceipt": _receipt(status="0x0"),
            "eth_blockNumber": "0x100",
        }
    )
    provider = PolygonProvider(client=client)  # type: ignore[arg-type]
    result = provider.fetch_usdt_transfer(TX_HASH)
    assert result.status == "reverted"
