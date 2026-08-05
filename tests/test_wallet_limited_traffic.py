"""ADR 018 Phase 2 Limited Traffic cohort tests (Cutover PR4)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.billing.models import CreditLedgerEntry, DepositRequest
from apps.billing.services.deposit_info import get_deposit_info
from apps.billing.services.deposit_verification import DepositVerificationService
from apps.wallet.models import DepositObservation, ObservationStatus
from apps.wallet.services import (
    WalletAllocationService,
    collect_wallet_metrics,
    should_expose_wallet_address,
    should_use_wallet_money_path,
)
from apps.wallet.services.conversion import observation_idempotency_key
from apps.wallet.services.flags import (
    cutover_ops_snapshot,
    parse_cutover_cohort_account_ids,
)
from shared.providers.blockchain import TransferResult

User = get_user_model()

_MNEMONIC = "test test test test test test test test test test test junk"
_USDT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
_PLATFORM = "0x2222222222222222222222222222222222222222"
_TX = "0x" + ("ef" * 32)


class _FakeBlockchainProvider:
    def __init__(self, result: TransferResult) -> None:
        self._result = result
        self.last_to_address: str | None = None

    def fetch_usdt_transfer(
        self, tx_hash: str, *, to_address: str | None = None
    ) -> TransferResult:
        self.last_to_address = to_address
        return self._result


def _transfer(*, amount: Decimal, to_address: str) -> TransferResult:
    return TransferResult(
        tx_hash=_TX,
        from_address="0x1111111111111111111111111111111111111111",
        to_address=to_address,
        amount=amount,
        confirmations=30,
        block_number=100,
        token_contract=_USDT,
        status="success",
        raw_rpc_response={
            "receipt": {"status": "0x1"},
            "matched_log": {"logIndex": "0x0"},
        },
    )


def _auth_headers(client: Client, user) -> dict[str, str]:
    from rest_framework_simplejwt.tokens import RefreshToken

    token = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
def test_empty_cohort_is_instant_legacy_rollback() -> None:
    user = User.objects.create_user(email="lt-empty@example.com", password="secret123")
    with override_settings(
        WALLET_ADDRESS_ENABLED=True,
        OBSERVATION_ENABLED=True,
        CREDIT_CONVERSION_V2=True,
        WALLET_CUTOVER_COHORT_ACCOUNT_IDS="",
        WALLET_HD_MNEMONIC=_MNEMONIC,
    ):
        assert should_expose_wallet_address(user.billing_account.pk) is False
        assert should_use_wallet_money_path(user.billing_account.pk) is False
        assert cutover_ops_snapshot()["cutover_rollback_status"] == "legacy_only"
        assert cutover_ops_snapshot()["cutover_cohort_size"] == 0


@pytest.mark.django_db
@override_settings(
    WALLET_ADDRESS_ENABLED=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_PLATFORM_WALLET=_PLATFORM,
    POLYGON_USDT_CONTRACT=_USDT,
    POLYGON_CHAIN_ID=137,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_DECIMALS=6,
    BILLING_ENABLED=True,
)
def test_deposit_info_wallet_address_only_for_cohort() -> None:
    in_user = User.objects.create_user(email="lt-in@example.com", password="secret123")
    out_user = User.objects.create_user(
        email="lt-out@example.com", password="secret123"
    )
    cohort = str(in_user.billing_account.pk)
    with override_settings(WALLET_CUTOVER_COHORT_ACCOUNT_IDS=cohort):
        in_info = get_deposit_info(account=in_user.billing_account)
        out_info = get_deposit_info(account=out_user.billing_account)
        addr = WalletAllocationService().ensure_active_address(in_user.billing_account)
        assert in_info["wallet"] == addr.address
        assert out_info["wallet"] == _PLATFORM
        assert addr.address in in_info["eip681_uri"]


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    WALLET_ADDRESS_ENABLED=True,
    OBSERVATION_ENABLED=True,
    CREDIT_CONVERSION_V2=True,
    SHADOW_MODE=False,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_CONTRACT=_USDT,
    POLYGON_PLATFORM_WALLET=_PLATFORM,
)
def test_limited_traffic_verify_uses_conversion_not_legacy_key() -> None:
    user = User.objects.create_user(email="lt-path@example.com", password="secret123")
    account = user.billing_account
    addr = WalletAllocationService().ensure_active_address(account)
    amount = Decimal("20.000000")
    provider = _FakeBlockchainProvider(
        _transfer(amount=amount, to_address=addr.address)
    )

    with override_settings(WALLET_CUTOVER_COHORT_ACCOUNT_IDS=str(account.pk)):
        svc = DepositVerificationService(
            blockchain_provider=provider,  # type: ignore[arg-type]
            min_confirmations=20,
        )
        deposit = svc.verify(
            account,
            tx_hash=_TX,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
            amount_requested=amount,
            idempotency_key="lt-1",
        )

        assert deposit.status == DepositRequest.Status.COMPLETED
        assert provider.last_to_address == addr.address
        assert not CreditLedgerEntry.objects.filter(
            idempotency_key=f"deposit:{deposit.pk}"
        ).exists()
        obs = DepositObservation.objects.get(tx_hash=_TX)
        assert obs.shadow_only is False
        assert obs.status == ObservationStatus.CREDITED
        assert CreditLedgerEntry.objects.filter(
            idempotency_key=observation_idempotency_key(obs)
        ).exists()
        account.refresh_from_db()
        assert account.balance == amount

        metrics = collect_wallet_metrics()
        assert metrics.cutover_cohort_size == 1
        assert metrics.cutover_cohort_deposits_completed == 1
        assert metrics.cutover_limited_traffic_active is True
        assert metrics.cutover_rollback_status == "limited_traffic"


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    WALLETCONNECT_ENABLED=True,
    WALLET_ADDRESS_ENABLED=True,
    WALLET_HD_MNEMONIC=_MNEMONIC,
    POLYGON_PLATFORM_WALLET=_PLATFORM,
    POLYGON_USDT_CONTRACT=_USDT,
    POLYGON_CHAIN_ID=137,
    POLYGON_MIN_CONFIRMATIONS=20,
    POLYGON_USDT_DECIMALS=6,
    SUBSCRIPTIONS_ENABLED=False,
    VOUCHERS_ENABLED=False,
)
def test_deposit_info_http_cohort(client: Client) -> None:
    user = User.objects.create_user(email="lt-http@example.com", password="secret123")
    with override_settings(
        WALLET_CUTOVER_COHORT_ACCOUNT_IDS=str(user.billing_account.pk)
    ):
        response = client.get(
            "/api/v1/billing/deposit-info/",
            **_auth_headers(client, user),
        )
    assert response.status_code == 200
    addr = WalletAllocationService().ensure_active_address(user.billing_account)
    assert response.json()["wallet"] == addr.address


def test_parse_cohort_skips_invalid_tokens() -> None:
    ids = parse_cutover_cohort_account_ids(
        "not-a-uuid, 11111111-1111-1111-1111-111111111111 , "
    )
    assert len(ids) == 1
