"""Tests for voucher redeem (ADR 011 PR1)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.exceptions import (
    UnsupportedRewardType,
    VoucherExpiredError,
    VoucherInvalidError,
    VoucherLimitError,
    VoucherReservedError,
    VoucherRevokedError,
)
from apps.billing.models import (
    AppendOnlyViolation,
    CreditLedgerEntry,
    LedgerReferenceType,
    RedemptionMode,
    RewardType,
    SoftDeleteViolation,
    Voucher,
    VoucherCampaign,
    VoucherRedemption,
    VoucherType,
)
from apps.billing.services.voucher_redeem import (
    issue_shared_campaign,
    issue_unique_voucher,
    voucher_redeem_service,
)
from apps.billing.voucher_codes import (
    assert_code_available,
    is_reserved_voucher_code,
    normalize_voucher_code,
)
from shared.events.billing_events import VoucherRedeemed
from shared.events.event_bus import event_bus

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="voucher-user@example.com", password=PASSWORD)


def _auth_headers(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


# --- normalize ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" summer2027 ", "SUMMER2027"),
        ("Summer2027", "SUMMER2027"),
        ("SUMMER2027", "SUMMER2027"),
        ("ｓｕｍｍｅｒ", "SUMMER"),  # NFKC fullwidth → ascii
        ("", ""),
        ("  ", ""),
    ],
)
def test_normalize_voucher_code(raw: str, expected: str) -> None:
    assert normalize_voucher_code(raw) == expected


def test_reserved_codes_constant_time_membership() -> None:
    assert is_reserved_voucher_code("admin")
    assert is_reserved_voucher_code(" FREE ")
    assert not is_reserved_voucher_code("SUMMER2027")


# --- models ---


@pytest.mark.django_db
def test_soft_delete_forbidden_on_voucher_entities(user: User) -> None:
    campaign = issue_shared_campaign(code="SOFTDEL1", credit_amount=Decimal("1"))
    voucher = issue_unique_voucher(code="RK-SOFT-1", credit_amount=Decimal("1"))
    with pytest.raises(SoftDeleteViolation):
        campaign.delete()
    with pytest.raises(SoftDeleteViolation):
        voucher.delete()


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_redemption_append_only_except_ledger_entry_id(user: User) -> None:
    issue_unique_voucher(code="RK-APPEND-1", credit_amount=Decimal("5"))
    result = voucher_redeem_service.redeem(
        account=user.billing_account,
        code="RK-APPEND-1",
        request_id="rid-append",
    )
    redemption = VoucherRedemption.objects.get(pk=result.redemption_id)
    redemption.amount = Decimal("9")
    with pytest.raises(AppendOnlyViolation):
        redemption.save()


# --- service ---


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_redeem_unique_credits_account(user: User) -> None:
    issue_unique_voucher(code="RK-UNIQUE-1", credit_amount=Decimal("25.500000"))
    received: list[VoucherRedeemed] = []
    event_bus.subscribe(VoucherRedeemed, received.append)
    try:
        result = voucher_redeem_service.redeem(
            account=user.billing_account,
            code=" rk-unique-1 ",
            request_id="req-unique-1",
            client_ip="1.2.3.4",
            user_agent="pytest",
        )
    finally:
        event_bus._handlers[VoucherRedeemed].remove(received.append)

    assert result.credited == Decimal("25.500000")
    assert result.balance == Decimal("25.500000")
    assert result.request_id == "req-unique-1"
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("25.500000")
    assert (
        CreditLedgerEntry.objects.filter(
            account=user.billing_account,
            reference_type=LedgerReferenceType.VOUCHER,
        ).count()
        == 1
    )
    assert Voucher.objects.get(code="RK-UNIQUE-1").status == Voucher.Status.REDEEMED
    assert len(received) == 1
    assert received[0].request_id == "req-unique-1"
    assert received[0].event_version == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_redeem_shared_campaign(user: User) -> None:
    issue_shared_campaign(
        code="SUMMER2027",
        credit_amount=Decimal("10"),
        max_redemptions_total=100,
        max_redemptions_per_account=1,
    )
    result = voucher_redeem_service.redeem(
        account=user.billing_account,
        code="summer2027",
        request_id="req-shared-1",
    )
    assert result.credited == Decimal("10.000000")
    replay = voucher_redeem_service.redeem(
        account=user.billing_account,
        code="SUMMER2027",
        request_id="req-shared-2",
    )
    assert replay.replay is True
    assert replay.redemption_id == result.redemption_id
    assert VoucherRedemption.objects.filter(account=user.billing_account).count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            account=user.billing_account, reference_type=LedgerReferenceType.VOUCHER
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_redeem_unique_http_idempotent_replay(user: User) -> None:
    issue_unique_voucher(code="RK-IDEMP-1", credit_amount=Decimal("7"))
    first = voucher_redeem_service.redeem(
        account=user.billing_account, code="RK-IDEMP-1", request_id="a"
    )
    second = voucher_redeem_service.redeem(
        account=user.billing_account, code="RK-IDEMP-1", request_id="b"
    )
    assert second.replay is True
    assert second.credited == first.credited
    assert second.redemption_id == first.redemption_id


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_redeem_reserved_expired_revoked(user: User) -> None:
    with pytest.raises(VoucherReservedError):
        voucher_redeem_service.redeem(
            account=user.billing_account, code="ADMIN", request_id="r"
        )
    with pytest.raises(VoucherInvalidError):
        voucher_redeem_service.redeem(
            account=user.billing_account, code="NOPE", request_id="r"
        )

    expired = issue_unique_voucher(code="RK-EXP-1", credit_amount=Decimal("1"))
    expired.expires_at = timezone.now() - timedelta(hours=1)
    expired.save(update_fields=["expires_at", "updated_at"])
    with pytest.raises(VoucherExpiredError):
        voucher_redeem_service.redeem(
            account=user.billing_account, code="RK-EXP-1", request_id="r"
        )

    revoked = issue_unique_voucher(code="RK-REV-1", credit_amount=Decimal("1"))
    revoked.status = Voucher.Status.REVOKED
    revoked.save(update_fields=["status", "updated_at"])
    with pytest.raises(VoucherRevokedError):
        voucher_redeem_service.redeem(
            account=user.billing_account, code="RK-REV-1", request_id="r"
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_unsupported_reward_type(user: User) -> None:
    campaign = issue_shared_campaign(code="BADREWARD", credit_amount=Decimal("1"))
    VoucherCampaign.objects.filter(pk=campaign.pk).update(
        reward_type=RewardType.PERCENT_BONUS
    )
    with pytest.raises(UnsupportedRewardType):
        voucher_redeem_service.redeem(
            account=user.billing_account, code="BADREWARD", request_id="r"
        )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_shared_total_limit(user: User) -> None:
    issue_shared_campaign(
        code="LIMIT1",
        credit_amount=Decimal("1"),
        max_redemptions_total=1,
        max_redemptions_per_account=1,
    )
    other = User.objects.create_user(email="other@example.com", password=PASSWORD)
    voucher_redeem_service.redeem(
        account=user.billing_account, code="LIMIT1", request_id="r1"
    )
    with pytest.raises(VoucherLimitError):
        voucher_redeem_service.redeem(
            account=other.billing_account, code="LIMIT1", request_id="r2"
        )


@pytest.mark.django_db(transaction=True)
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_shared_parallel_redeems_respect_total_limit() -> None:
    """Parallel SHARED redeems must not oversell max_redemptions_total."""
    from django.db import connection as db_connection

    db_connection.ensure_connection()
    # Create directly (avoid cross-table code probe) — mirrors credit concurrency tests.
    campaign = VoucherCampaign.objects.create(
        code="TORTURE100",
        redemption_mode=RedemptionMode.SHARED,
        voucher_type=VoucherType.PROMO,
        reward_type=RewardType.FIXED_CREDIT,
        credit_amount=Decimal("1.000000"),
        max_redemptions_total=10,
        max_redemptions_per_account=1,
        status=VoucherCampaign.Status.ACTIVE,
    )
    users = [
        User.objects.create_user(email=f"t{i}@example.com", password=PASSWORD)
        for i in range(40)
    ]
    account_ids = [u.billing_account.pk for u in users]

    def _attempt(account_id) -> bool:
        from apps.billing.models import Account

        try:
            account = Account.objects.get(pk=account_id)
            voucher_redeem_service.redeem(
                account=account,
                code="TORTURE100",
                request_id=f"torture-{account_id}",
            )
            return True
        except VoucherLimitError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_attempt, account_ids))

    successes = sum(1 for ok in results if ok)
    redemptions = VoucherRedemption.objects.filter(campaign=campaign)
    assert (
        redemptions.count() == 10
    ), f"redemptions={redemptions.count()} successes={successes}"
    assert successes == 10
    redemption_ids = [str(pk) for pk in redemptions.values_list("id", flat=True)]
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.VOUCHER,
            reference_id__in=redemption_ids,
        ).count()
        == 10
    )


# --- API ---


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_api_redeem_success(client: Client, user: User) -> None:
    issue_unique_voucher(code="RK-API-1", credit_amount=Decimal("12.000000"))
    response = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "rk-api-1"}),
        content_type="application/json",
        HTTP_X_REQUEST_ID="http-req-1",
        **_auth_headers(user),
    )
    assert response.status_code == 200
    assert response.json() == {
        "credited": "12.000000",
        "balance": "12.000000",
    }
    redemption = VoucherRedemption.objects.get()
    assert redemption.request_id == "http-req-1"


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_api_redeem_double_post_idempotent(client: Client, user: User) -> None:
    issue_unique_voucher(code="RK-API-IDEM", credit_amount=Decimal("3"))
    headers = _auth_headers(user)
    r1 = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "RK-API-IDEM"}),
        content_type="application/json",
        **headers,
    )
    r2 = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "RK-API-IDEM"}),
        content_type="application/json",
        **headers,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["credited"] == r2.json()["credited"]
    assert VoucherRedemption.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.VOUCHER
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=False)
def test_api_vouchers_disabled_returns_404(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "ANY"}),
        content_type="application/json",
        **_auth_headers(user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_api_requires_auth(client: Client) -> None:
    assert (
        client.post(
            "/api/v1/billing/vouchers/redeem/",
            data=json.dumps({"code": "X"}),
            content_type="application/json",
        ).status_code
        == 401
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_api_invalid_returns_structured_error(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "MISSING"}),
        content_type="application/json",
        **_auth_headers(user),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "voucher_invalid"
    assert "detail" in body


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    VOUCHERS_ENABLED=True,
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework_simplejwt.authentication.JWTAuthentication",
        ],
        "DEFAULT_PERMISSION_CLASSES": [
            "rest_framework.permissions.IsAuthenticated",
        ],
        "DEFAULT_THROTTLE_RATES": {"billing_voucher_redeem": "2/5min"},
        "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    },
)
def test_api_throttle_429(client: Client, user: User) -> None:
    from django.core.cache import cache

    cache.clear()
    headers = _auth_headers(user)
    for i in range(2):
        issue_unique_voucher(code=f"RK-TH-{i}", credit_amount=Decimal("1"))
        assert (
            client.post(
                "/api/v1/billing/vouchers/redeem/",
                data=json.dumps({"code": f"RK-TH-{i}"}),
                content_type="application/json",
                **headers,
            ).status_code
            == 200
        )
    issue_unique_voucher(code="RK-TH-BLOCK", credit_amount=Decimal("1"))
    blocked = client.post(
        "/api/v1/billing/vouchers/redeem/",
        data=json.dumps({"code": "RK-TH-BLOCK"}),
        content_type="application/json",
        **headers,
    )
    assert blocked.status_code == 429


# --- events / arch ---


def test_voucher_redeemed_event_contract_snapshot() -> None:
    names = {f.name for f in fields(VoucherRedeemed)}
    assert names >= {
        "event_version",
        "voucher_id",
        "campaign_id",
        "redemption_id",
        "account_id",
        "amount",
        "balance_after",
        "ledger_entry_id",
        "request_id",
        "redeemed_at",
    }


@pytest.mark.django_db
def test_assert_code_available_cross_table(user: User) -> None:
    issue_shared_campaign(code="CROSS1", credit_amount=Decimal("1"))
    with pytest.raises(ValueError, match="already used"):
        assert_code_available("cross1")
