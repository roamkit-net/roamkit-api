"""HTTP tests for GET/PUT/DELETE /me/esims/{id}/auto-topup/ (PR4)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.orders.models import Order
from shared.providers.esim import TopupPackage


class FakeTopupProvider:
    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return [
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

    def submit_topup(self, iccid: str, package_id: str):
        raise AssertionError("submit unused")

    def get_usage(self, iccid: str):
        raise AssertionError("usage unused")


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(
        email="auto-topup-api@example.com",
        password="SecurePass1!",
    )


@pytest.fixture
def other_user(db) -> User:
    return User.objects.create_user(
        email="other-auto-topup-api@example.com",
        password="SecurePass1!",
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-auto-topup-api",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def esim(user: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-auto-api",
        customer_ref="ref-auto-api",
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000007777",
        status=Esim.Status.ACTIVATED,
    )


@pytest.fixture
def other_esim(other_user: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=other_user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-auto-api-other",
        customer_ref="ref-auto-api-other",
    )
    return Esim.objects.create(
        user=other_user,
        order=order,
        iccid="891000000000007778",
        status=Esim.Status.ACTIVATED,
    )


def _auth(client: Client, user: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def _put_body(**overrides):
    body = {
        "package_id": "topup-1gb",
        "enabled": True,
        "trigger_mode": "usage_threshold",
        "threshold_mb": 500,
        "renew_mode": "until_funds",
    }
    body.update(overrides)
    return body


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_create_get_update_delete_policy(
    client: Client, user: User, esim: Esim, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeTopupProvider()
    )
    headers = _auth(client, user)

    assert (
        client.get(f"/api/v1/me/esims/{esim.pk}/auto-topup/", **headers).status_code
        == 404
    )

    create = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/",
        data=json.dumps(_put_body()),
        content_type="application/json",
        **headers,
    )
    assert create.status_code == 201, create.content
    payload = create.json()
    assert payload["package_id"] == "topup-1gb"
    assert payload["version"] == 0
    assert payload["trigger_mode"] == "usage_threshold"
    assert payload["threshold_mb"] == 500

    got = client.get(f"/api/v1/me/esims/{esim.pk}/auto-topup/", **headers)
    assert got.status_code == 200
    assert got.json()["id"] == payload["id"]

    update = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/",
        data=json.dumps(_put_body(renew_mode="fixed_count", remaining_count=3)),
        content_type="application/json",
        HTTP_IF_MATCH='"0"',
        **headers,
    )
    assert update.status_code == 200, update.content
    assert update.json()["version"] == 1
    assert update.json()["remaining_count"] == 3

    conflict = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/",
        data=json.dumps(_put_body(version=0)),
        content_type="application/json",
        **headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "VERSION_CONFLICT"

    deleted = client.delete(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/?version=1",
        **headers,
    )
    assert deleted.status_code == 204
    assert not EsimAutoTopupPolicy.objects.filter(esim=esim).exists()


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_hides_other_users_esim(
    client: Client, user: User, other_esim: Esim, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeTopupProvider()
    )
    headers = _auth(client, user)
    response = client.put(
        f"/api/v1/me/esims/{other_esim.pk}/auto-topup/",
        data=json.dumps(_put_body()),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=False,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_mutate_404_when_flag_off(
    client: Client, user: User, esim: Esim, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeTopupProvider()
    )
    headers = _auth(client, user)
    response = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/",
        data=json.dumps(_put_body()),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_unknown_package_404(
    client: Client, user: User, esim: Esim, monkeypatch
) -> None:
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeTopupProvider()
    )
    headers = _auth(client, user)
    response = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/",
        data=json.dumps(_put_body(package_id="missing")),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 404
