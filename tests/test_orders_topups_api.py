"""Tests for POST /api/v1/orders/ and POST /api/v1/me/esims/{id}/topups/."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Package
from apps.esims.models import Esim, Topup
from apps.orders.models import Order
from shared.providers.esim import (
    OrderedSimDTO,
    OrderResult,
    TopupPackage,
    TopupResult,
    UsageDTO,
)

User = get_user_model()


class FakeOrderProvider:
    def __init__(
        self, result: OrderResult | None = None, *, fail: bool = False
    ) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls.append((package_id, customer_ref))
        if self.fail:
            raise RuntimeError("provider unavailable")
        assert self.result is not None
        return self.result


class FakeTopupProvider:
    def __init__(
        self,
        *,
        result: TopupResult | None = None,
        fail: bool = False,
    ) -> None:
        self.result = result
        self.fail = fail
        self.topups = [
            TopupPackage(
                external_id="topup-1gb",
                title="1 GB Top-up",
                data_allowance="1 GB",
                validity_days=7,
                price_usd=Decimal("5.00"),
                net_price_usd=Decimal("4.50"),
                is_unlimited=False,
                plan_type="topup",
            )
        ]

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        if self.fail:
            raise RuntimeError("provider unavailable")
        assert self.result is not None
        return self.result

    def get_usage(self, iccid: str) -> UsageDTO:
        raise AssertionError("unused")


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="buyer@example.com", password="SecurePass1!")


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def order_result() -> OrderResult:
    return OrderResult(
        external_order_id="9666",
        code="20230227-009666",
        package_id="pkg-us-1gb-7d",
        customer_ref="rk",
        currency="USD",
        price_usd=Decimal("9.50"),
        manual_installation="<p>Manual</p>",
        qrcode_installation="<p>QR</p>",
        installation_guide_url="https://sandbox.airalo.com/installation-guide",
        sims=[
            OrderedSimDTO(
                iccid="891000000000009125",
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


def _auth_headers(client: Client, user: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def _fund(user: User, amount: str) -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="api-fund",
        idempotency_key=f"api-fund:{user.pk}:{amount}",
    )


def _make_esim(user: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-1",
        customer_ref="ref-1",
    )
    return Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid="891000000000009125",
        status=Esim.Status.ACTIVATED,
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_create_order_requires_auth(client: Client, package: Package) -> None:
    response = client.post(
        "/api/v1/orders/",
        data=json.dumps(
            {
                "package_id": package.external_id,
                "idempotency_key": "auth-check",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_create_order_success(
    client: Client,
    user: User,
    package: Package,
    order_result: OrderResult,
    monkeypatch,
) -> None:
    _fund(user, "20.00")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = client.post(
        "/api/v1/orders/",
        data=json.dumps(
            {
                "package_id": package.external_id,
                "idempotency_key": "api-order-1",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == Order.Status.FULFILLED
    assert payload["package_id"] == package.external_id
    assert payload["idempotency_key"] == "api-order-1"
    assert len(payload["esims"]) == 1
    assert payload["esims"][0]["iccid"] == "891000000000009125"
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("8.500000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_create_order_insufficient_funds(
    client: Client, user: User, package: Package, order_result: OrderResult, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )
    response = client.post(
        "/api/v1/orders/",
        data=json.dumps(
            {
                "package_id": package.external_id,
                "idempotency_key": "api-order-underfunded",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 402
    payload = response.json()
    assert payload["code"] == "INSUFFICIENT_CREDITS"
    assert payload["required"] == "11.500000"
    assert payload["balance"] == "0.000000"
    assert payload["missing"] == "11.500000"
    assert Order.objects.count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_create_order_idempotent_retry(
    client: Client,
    user: User,
    package: Package,
    order_result: OrderResult,
    monkeypatch,
) -> None:
    _fund(user, "20.00")
    provider = FakeOrderProvider(order_result)
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: provider,
    )
    body = json.dumps(
        {
            "package_id": package.external_id,
            "idempotency_key": "api-order-retry",
        }
    )
    headers = _auth_headers(client, user)

    first = client.post(
        "/api/v1/orders/",
        data=body,
        content_type="application/json",
        **headers,
    )
    second = client.post(
        "/api/v1/orders/",
        data=body,
        content_type="application/json",
        **headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert Order.objects.count() == 1
    assert len(provider.calls) == 1
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("8.500000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=False)
def test_create_order_hidden_when_billing_disabled(
    client: Client, user: User, package: Package
) -> None:
    response = client.post(
        "/api/v1/orders/",
        data=json.dumps(
            {
                "package_id": package.external_id,
                "idempotency_key": "api-order-disabled",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_topup_success(
    client: Client, user: User, package: Package, monkeypatch
) -> None:
    _fund(user, "10.00")
    esim = _make_esim(user, package)
    result = TopupResult(
        external_order_id="top-1",
        code="T1",
        package_id="topup-1gb",
        iccid=esim.iccid,
        currency="USD",
        price_usd=Decimal("5.00"),
        customer_ref="ref",
    )
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=result),
    )

    response = client.post(
        f"/api/v1/me/esims/{esim.pk}/topups/",
        data=json.dumps(
            {
                "package_id": "topup-1gb",
                "idempotency_key": "api-topup-1",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == Topup.Status.FULFILLED
    assert payload["package_external_id"] == "topup-1gb"
    assert payload["amount"] == "5.000000"
    assert payload["idempotency_key"] == "api-topup-1"
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("5.000000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_topup_insufficient_funds(
    client: Client, user: User, package: Package, monkeypatch
) -> None:
    esim = _make_esim(user, package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=None),
    )
    response = client.post(
        f"/api/v1/me/esims/{esim.pk}/topups/",
        data=json.dumps(
            {
                "package_id": "topup-1gb",
                "idempotency_key": "api-topup-underfunded",
            }
        ),
        content_type="application/json",
        **_auth_headers(client, user),
    )
    assert response.status_code == 402
    payload = response.json()
    assert payload["code"] == "INSUFFICIENT_CREDITS"
    assert payload["required"] == "5.000000"
    assert payload["balance"] == "0.000000"
    assert payload["missing"] == "5.000000"
    assert Topup.objects.count() == 0
