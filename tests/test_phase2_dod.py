"""Phase 2 DoD: register → sandbox eSIM → me/esims (+ usage + isolation)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from shared.providers.esim import OrderedSimDTO, OrderResult, UsageDTO

User = get_user_model()

PASSWORD = "SecurePass1!"
PACKAGE_ID = "pkg-us-1gb-7d"
ICCID = "891000000000009125"


class FakeOrderProvider:
    def __init__(self, result: OrderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls.append((package_id, customer_ref))
        return self.result


class FakeTopupProvider:
    def __init__(self, usage: UsageDTO) -> None:
        self.usage = usage
        self.usage_calls: list[str] = []

    def get_usage(self, iccid: str) -> UsageDTO:
        self.usage_calls.append(iccid)
        return self.usage

    def list_topups(self, iccid: str) -> list[Any]:
        return []

    def submit_topup(self, iccid: str, package_id: str) -> Any:
        raise AssertionError("submit_topup must not be called in Phase 2")


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id=PACKAGE_ID,
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def order_result() -> OrderResult:
    return OrderResult(
        external_order_id="9666",
        code="20230227-009666",
        package_id=PACKAGE_ID,
        customer_ref="sandbox:dod@example.com",
        currency="USD",
        price_usd=Decimal("9.50"),
        manual_installation="<p>Manual install</p>",
        qrcode_installation="<p>QR install</p>",
        installation_guide_url="https://sandbox.airalo.com/installation-guide",
        sims=[
            OrderedSimDTO(
                iccid=ICCID,
                lpa="lpa.airalo.com",
                matching_id="TEST",
                qrcode="LPA:1$lpa.airalo.com$TEST",
                qrcode_url="https://sandbox.airalo.com/qr?id=1",
                direct_apple_installation_url=(
                    "https://esimsetup.apple.com/esim_qrcode_provisioning"
                    "?carddata=LPA:1$lpa.airalo.com$TEST"
                ),
            )
        ],
    )


@pytest.mark.django_db
def test_phase2_dod_register_sandbox_me_esims(
    client: Client,
    package: Package,
    order_result: OrderResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end Phase 2 DoD path used for staging verification."""
    email = "dod@example.com"

    register = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert register.status_code == 201
    assert register.json()["email"] == email

    token = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert token.status_code == 200
    access = token.json()["access"]
    assert access

    monkeypatch.setattr(
        "apps.orders.management.commands.create_sandbox_esim.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )
    call_command("create_sandbox_esim", email=email, package_id=package.external_id)

    esim = Esim.objects.get(iccid=ICCID)
    assert esim.user.email == email

    listing = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["count"] == 1
    item = payload["results"][0]
    assert item["id"] == esim.pk
    assert item["iccid"] == ICCID
    assert item["qrcode"] == "LPA:1$lpa.airalo.com$TEST"
    assert item["qrcode_url"] == "https://sandbox.airalo.com/qr?id=1"
    assert item["manual_installation"] == "<p>Manual install</p>"
    assert item["qrcode_installation"] == "<p>QR install</p>"
    assert item["direct_apple_installation_url"].startswith(
        "https://esimsetup.apple.com/"
    )

    usage_provider = FakeTopupProvider(
        UsageDTO(
            remaining_mb=750,
            total_mb=1024,
            expired_at="2026-12-31 23:59:59",
            is_unlimited=False,
            status="ACTIVE",
            remaining_voice=0,
            remaining_text=0,
            total_voice=0,
            total_text=0,
        )
    )
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: usage_provider,
    )

    usage = client.get(
        f"/api/v1/me/esims/{esim.pk}/usage/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert usage.status_code == 200
    assert usage.json()["remaining_mb"] == 750
    assert usage.json()["status"] == "ACTIVE"
    assert usage_provider.usage_calls == [ICCID]

    other = User.objects.create_user(email="other@example.com", password=PASSWORD)
    other_token = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": other.email, "password": PASSWORD}),
        content_type="application/json",
    )
    other_access = other_token.json()["access"]

    denied = client.get(
        f"/api/v1/me/esims/{esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {other_access}",
    )
    assert denied.status_code == 404

    other_list = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {other_access}",
    )
    assert other_list.status_code == 200
    assert other_list.json()["count"] == 0
