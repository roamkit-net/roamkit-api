"""Tests for /api/v1/billing/* endpoints (PR5)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.billing.exceptions import (
    DepositVerificationFailedError,
    DuplicateTransactionError,
    InsufficientConfirmationsError,
)
from apps.billing.models import DepositRequest, LedgerReferenceType
from apps.billing.services.credit import credit_service
from apps.billing.services.deposit_info import build_eip681_uri
from apps.billing.services.deposit_verification import DepositVerificationService
from shared.providers.blockchain import BlockchainRPCError, TransferResult

User = get_user_model()

PASSWORD = "SecurePass1!"
TX_HASH = "0x" + ("ab" * 32)
PLATFORM_WALLET = "0x2222222222222222222222222222222222222222"
USDT_CONTRACT = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"

_POLYGON = {
    "POLYGON_PLATFORM_WALLET": PLATFORM_WALLET,
    "POLYGON_USDT_CONTRACT": USDT_CONTRACT,
    "POLYGON_CHAIN_ID": 137,
    "POLYGON_MIN_CONFIRMATIONS": 20,
    "POLYGON_USDT_DECIMALS": 6,
}


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="alice@example.com", password=PASSWORD)


def _access_token(client: Client, email: str, password: str = PASSWORD) -> str:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": password}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["access"]


def _auth_headers(client: Client, user: User) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {_access_token(client, user.email)}"}


def _transfer(**kwargs: Any) -> TransferResult:
    defaults = {
        "tx_hash": TX_HASH,
        "from_address": "0x1111111111111111111111111111111111111111",
        "to_address": PLATFORM_WALLET.lower(),
        "amount": Decimal("10.000000"),
        "confirmations": 30,
        "block_number": 100,
        "token_contract": USDT_CONTRACT.lower(),
        "status": "success",
        "raw_rpc_response": {"ok": True},
    }
    defaults.update(kwargs)
    return TransferResult(**defaults)


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_balance_requires_authentication(client: Client) -> None:
    assert client.get("/api/v1/billing/balance/").status_code == 401


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_balance_returns_account_balance(client: Client, user: User) -> None:
    credit_service.credit(
        user.billing_account,
        Decimal("15.500000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="balance-api-fixture",
        idempotency_key="balance-api-fixture",
    )

    response = client.get("/api/v1/billing/balance/", **_auth_headers(client, user))

    assert response.status_code == 200
    assert response.json() == {"balance": "15.500000"}


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=False)
def test_balance_hidden_when_billing_disabled(client: Client, user: User) -> None:
    response = client.get("/api/v1/billing/balance/", **_auth_headers(client, user))
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    WALLETCONNECT_ENABLED=True,
    SUBSCRIPTIONS_ENABLED=False,
    **_POLYGON,
)
def test_deposit_info_full_meta(client: Client, user: User) -> None:
    response = client.get(
        "/api/v1/billing/deposit-info/",
        **_auth_headers(client, user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["wallet"] == PLATFORM_WALLET
    assert payload["chain_id"] == 137
    assert payload["token_symbol"] == "USDT"
    assert payload["token_decimals"] == 6
    assert payload["contract"] == USDT_CONTRACT
    assert payload["min_confirmations"] == 20
    assert payload["walletconnect_enabled"] is True
    assert payload["subscriptions_enabled"] is False
    assert payload["eip681_uri"] == build_eip681_uri(
        wallet=PLATFORM_WALLET,
        contract=USDT_CONTRACT,
        chain_id=137,
    )
    assert "transfer?address=" in payload["eip681_uri"]
    assert "@137/" in payload["eip681_uri"]


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=False, **_POLYGON)
def test_deposit_info_hidden_when_billing_disabled(client: Client, user: User) -> None:
    response = client.get(
        "/api/v1/billing/deposit-info/",
        **_auth_headers(client, user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, WALLETCONNECT_ENABLED=False)
def test_verify_wallet_forbidden_when_walletconnect_disabled(
    client: Client, user: User
) -> None:
    response = client.post(
        "/api/v1/billing/verify-wallet/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "wc-1",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, WALLETCONNECT_ENABLED=True)
def test_verify_wallet_success(client: Client, user: User, monkeypatch) -> None:
    deposit = DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("10.000000"),
        amount_credited=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        tx_hash=TX_HASH,
        idempotency_key="wc-ok",
        status=DepositRequest.Status.COMPLETED,
    )

    mock_verify = MagicMock(return_value=deposit)
    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        mock_verify,
    )

    response = client.post(
        "/api/v1/billing/verify-wallet/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "wc-ok",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["payment_method"] == "wallet_connect"
    assert payload["tx_hash"] == TX_HASH
    mock_verify.assert_called_once()
    call_kwargs = mock_verify.call_args
    assert call_kwargs.kwargs["payment_method"] == "wallet_connect"
    assert call_kwargs.kwargs["idempotency_key"] == "wc-ok"


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_cex_success(client: Client, user: User, monkeypatch) -> None:
    deposit = DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("25.000000"),
        amount_credited=Decimal("25.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash=TX_HASH,
        idempotency_key="cex-ok",
        status=DepositRequest.Status.COMPLETED,
    )
    mock_verify = MagicMock(return_value=deposit)
    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        mock_verify,
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "25.000000",
                "idempotency_key": "cex-ok",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 200
    assert response.json()["payment_method"] == "cex_manual"
    assert mock_verify.call_args.kwargs["payment_method"] == "cex_manual"


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_cex_insufficient_confirmations_returns_202(
    client: Client, user: User, monkeypatch
) -> None:
    pending = DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash=TX_HASH,
        idempotency_key="cex-pending",
        status=DepositRequest.Status.PENDING,
    )

    def _raise(*args: Any, **kwargs: Any) -> DepositRequest:
        raise InsufficientConfirmationsError(5, 20)

    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        _raise,
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-pending",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["id"] == str(pending.pk)
    assert payload["status"] == "pending"
    assert payload["confirmations"] == 5
    assert payload["required_confirmations"] == 20


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_cex_duplicate_returns_409(
    client: Client, user: User, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        MagicMock(side_effect=DuplicateTransactionError("already credited")),
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-dup",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 409
    assert "already credited" in response.json()["detail"]


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_cex_failed_returns_400(client: Client, user: User, monkeypatch) -> None:
    failed = DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash=TX_HASH,
        idempotency_key="cex-fail",
        status=DepositRequest.Status.FAILED,
        failure_reason="Amount mismatch",
    )

    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        MagicMock(side_effect=DepositVerificationFailedError("Amount mismatch")),
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-fail",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 400
    assert response.json()["id"] == str(failed.pk)
    assert response.json()["status"] == "failed"


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_cex_rpc_error_returns_502(
    client: Client, user: User, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service.verify",
        MagicMock(side_effect=BlockchainRPCError("timeout")),
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-rpc",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 502


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_verify_rejects_invalid_body(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps({"tx_hash": TX_HASH}),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 400
    body = response.json()
    assert "amount_requested" in body
    assert "idempotency_key" in body


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=False)
def test_verify_cex_hidden_when_billing_disabled(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-off",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    POLYGON_MIN_CONFIRMATIONS=20,
)
def test_verify_cex_integration_with_fake_provider(
    client: Client, user: User, monkeypatch
) -> None:
    """End-to-end through DepositVerificationService (not just the view mock)."""
    provider = MagicMock()
    provider.fetch_usdt_transfer.return_value = _transfer()
    service = DepositVerificationService(
        blockchain_provider=provider,
        min_confirmations=20,
    )
    monkeypatch.setattr(
        "apps.billing.views.deposit_verification_service",
        service,
    )

    response = client.post(
        "/api/v1/billing/verify-cex/",
        data=json.dumps(
            {
                "tx_hash": TX_HASH,
                "amount_requested": "10.000000",
                "idempotency_key": "cex-e2e",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["amount_credited"] == "10.000000"
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("10.000000")


def test_build_eip681_uri_format() -> None:
    uri = build_eip681_uri(wallet=PLATFORM_WALLET, contract=USDT_CONTRACT, chain_id=137)
    assert uri == (f"ethereum:{USDT_CONTRACT}@137/transfer?address={PLATFORM_WALLET}")
