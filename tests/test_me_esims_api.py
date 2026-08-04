"""Tests for /api/v1/me/esims/ endpoints and object-level auth."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from shared.providers.esim import TopupPackage, UsageDTO

User = get_user_model()


class FakeTopupProvider:
    def __init__(
        self,
        *,
        usage: UsageDTO | None = None,
        topups: list[TopupPackage] | None = None,
    ) -> None:
        self.usage = usage or UsageDTO(
            remaining_mb=500,
            total_mb=1024,
            expired_at="2026-12-31 23:59:59",
            is_unlimited=False,
            status="ACTIVE",
            remaining_voice=0,
            remaining_text=0,
            total_voice=0,
            total_text=0,
        )
        self.topups = topups or [
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
        self.usage_calls: list[str] = []
        self.topup_calls: list[str] = []

    def get_usage(self, iccid: str) -> UsageDTO:
        self.usage_calls.append(iccid)
        return self.usage

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        self.topup_calls.append(iccid)
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> Any:
        raise AssertionError("submit_topup must not be called in Phase 2")


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="alice@example.com", password="SecurePass1!")


@pytest.fixture
def other_user(db) -> User:
    return User.objects.create_user(email="bob@example.com", password="SecurePass1!")


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
        net_price_usd=Decimal("6.30"),
        synced_at=timezone.now(),
    )


def _make_esim(*, user: User, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-4:]}",
        customer_ref=f"ref-{iccid[-4:]}",
        **product_snapshot_kwargs(package),
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid=iccid,
        lpa="lpa.airalo.com",
        matching_id="TEST",
        qrcode=f"LPA:1$lpa.airalo.com${iccid}",
        qrcode_url=f"https://sandbox.airalo.com/qr?id={iccid}",
        direct_apple_installation_url=(
            f"https://esimsetup.apple.com/esim_qrcode_provisioning"
            f"?carddata=LPA:1$lpa.airalo.com${iccid}"
        ),
        manual_installation="<p>Manual</p>",
        qrcode_installation="<p>QR</p>",
        installation_guide_url="https://sandbox.airalo.com/installation-guide",
        status=Esim.Status.PURCHASED,
    )


@pytest.fixture
def alice_esim(user: User, package: Package) -> Esim:
    return _make_esim(user=user, package=package, iccid="891000000000009125")


@pytest.fixture
def bob_esim(other_user: User, package: Package) -> Esim:
    return _make_esim(user=other_user, package=package, iccid="891000000000009999")


def _access_token(client: Client, email: str, password: str = "SecurePass1!") -> str:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": password}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()["access"]


@pytest.mark.django_db
def test_list_esims_requires_authentication(client: Client) -> None:
    response = client.get("/api/v1/me/esims/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_list_esims_returns_only_own(
    client: Client,
    user: User,
    alice_esim: Esim,
    bob_esim: Esim,
) -> None:
    access = _access_token(client, user.email)

    response = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert len(payload["results"]) == 1
    item = payload["results"][0]
    assert item["id"] == alice_esim.pk
    assert item["iccid"] == alice_esim.iccid
    assert item["qrcode"] == alice_esim.qrcode
    assert item["qrcode_url"] == alice_esim.qrcode_url
    assert item["direct_apple_installation_url"] == (
        alice_esim.direct_apple_installation_url
    )
    assert item["package_title"] == "1 GB - 7 Days"
    assert item["country_code"] == "US"
    assert item["data_allowance"] == "1 GB"
    assert item["validity_days"] == 7
    assert item["paid_usd"] == "11.50"
    assert item["currency"] == "USD"
    assert item["issued_at"]
    assert item["activated_at"] is None
    assert "net_price_usd" not in item
    assert "net_price" not in item
    assert bob_esim.iccid not in {row["iccid"] for row in payload["results"]}


@pytest.mark.django_db
def test_detail_returns_own_esim(client: Client, user: User, alice_esim: Esim) -> None:
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == alice_esim.pk
    assert payload["iccid"] == alice_esim.iccid
    assert payload["manual_installation"] == "<p>Manual</p>"
    assert payload["installation_guide_url"] == alice_esim.installation_guide_url
    assert payload["paid_usd"] == "11.50"
    assert payload["package_title"] == "1 GB - 7 Days"
    assert payload["issued_at"] == payload["created_at"]
    assert payload["activated_at"] is None
    assert "net_price_usd" not in payload


@pytest.mark.django_db
def test_detail_includes_activated_at_from_lifecycle(
    client: Client, user: User, alice_esim: Esim
) -> None:
    from apps.esims.services.lifecycle_service import lifecycle_service

    lifecycle_service.transition(alice_esim, Esim.Status.INSTALLATION_STARTED)
    lifecycle_service.transition(alice_esim, Esim.Status.INSTALLED)
    lifecycle_service.transition(alice_esim, Esim.Status.ACTIVATED)

    access = _access_token(client, user.email)
    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["activated_at"] is not None
    assert payload["status"] == Esim.Status.ACTIVATED


@pytest.mark.django_db
def test_detail_paid_usd_uses_order_snapshot_not_live_catalog(
    client: Client, user: User, alice_esim: Esim, package: Package
) -> None:
    package.price_usd = Decimal("99.00")
    package.save(update_fields=["price_usd", "updated_at"])

    access = _access_token(client, user.email)
    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    assert response.json()["paid_usd"] == "11.50"


@pytest.mark.django_db
def test_detail_hides_other_users_esim(
    client: Client, user: User, bob_esim: Esim
) -> None:
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{bob_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_usage_fetches_provider_and_updates_cache(
    client: Client,
    user: User,
    alice_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeTopupProvider()
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/usage/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["remaining_mb"] == 500
    assert payload["total_mb"] == 1024
    assert payload["status"] == "ACTIVE"
    assert provider.usage_calls == [alice_esim.iccid]

    alice_esim.refresh_from_db()
    assert alice_esim.usage_remaining_mb == 500
    assert alice_esim.usage_total_mb == 1024
    assert alice_esim.usage_status == "ACTIVE"
    assert alice_esim.usage_is_unlimited is False
    assert alice_esim.usage_synced_at is not None
    assert alice_esim.usage_expired_at is not None


@pytest.mark.django_db
def test_usage_hides_other_users_esim(
    client: Client,
    user: User,
    bob_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeTopupProvider()
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{bob_esim.pk}/usage/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 404
    assert provider.usage_calls == []


@pytest.mark.django_db
def test_topups_lists_packages_without_purchase(
    client: Client,
    user: User,
    alice_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeTopupProvider()
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/topups/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["results"]) == 1
    assert payload["results"][0]["id"] == "topup-1gb"
    assert payload["results"][0]["title"] == "1 GB Top-up"
    assert payload["results"][0]["price_usd"] == "5.00"
    assert provider.topup_calls == [alice_esim.iccid]


@pytest.mark.django_db
def test_topups_hides_other_users_esim(
    client: Client,
    user: User,
    bob_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeTopupProvider()
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{bob_esim.pk}/topups/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 404
    assert provider.topup_calls == []


@pytest.mark.django_db
def test_events_idempotent_and_owned(
    client: Client,
    user: User,
    alice_esim: Esim,
    bob_esim: Esim,
) -> None:
    access = _access_token(client, user.email)
    url = f"/api/v1/me/esims/{alice_esim.pk}/events/"
    body = {
        "event_type": "install.opened",
        "idempotency_key": "setup-open-1",
        "schema_version": 1,
        "resume_step": 1,
    }
    first = client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert first.status_code == 201
    second = client.post(
        url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    listed = client.get(url, HTTP_AUTHORIZATION=f"Bearer {access}")
    assert listed.status_code == 200
    assert any(e["event_type"] == "install.opened" for e in listed.json())

    other = client.post(
        f"/api/v1/me/esims/{bob_esim.pk}/events/",
        data=json.dumps({**body, "idempotency_key": "other"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert other.status_code == 404


def _patch_note(
    client: Client, *, access: str, esim_id: int, body: dict[str, Any]
) -> Any:
    return client.patch(
        f"/api/v1/me/esims/{esim_id}/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


@pytest.mark.django_db
def test_patch_note_owner_round_trip(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = _patch_note(
        client, access=access, esim_id=alice_esim.pk, body={"note": "Japan trip"}
    )
    assert response.status_code == 200
    assert response.json()["note"] == "Japan trip"

    detail = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert detail.status_code == 200
    assert detail.json()["note"] == "Japan trip"

    alice_esim.refresh_from_db()
    assert alice_esim.note == "Japan trip"


@pytest.mark.django_db
def test_patch_note_hides_other_users_esim(
    client: Client, user: User, bob_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = _patch_note(
        client, access=access, esim_id=bob_esim.pk, body={"note": "stolen"}
    )
    assert response.status_code == 404
    bob_esim.refresh_from_db()
    assert bob_esim.note == ""


@pytest.mark.django_db
def test_patch_note_strips_whitespace(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = _patch_note(
        client, access=access, esim_id=alice_esim.pk, body={"note": "   "}
    )
    assert response.status_code == 200
    assert response.json()["note"] == ""
    alice_esim.refresh_from_db()
    assert alice_esim.note == ""


@pytest.mark.django_db
def test_patch_note_rejects_too_long(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = _patch_note(
        client,
        access=access,
        esim_id=alice_esim.pk,
        body={"note": "x" * 256},
    )
    assert response.status_code == 400
    alice_esim.refresh_from_db()
    assert alice_esim.note == ""


@pytest.mark.django_db
def test_put_note_not_allowed(client: Client, user: User, alice_esim: Esim) -> None:
    access = _access_token(client, user.email)
    response = client.put(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        data=json.dumps({"note": "via put"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 405
    alice_esim.refresh_from_db()
    assert alice_esim.note == ""


@pytest.mark.django_db
def test_patch_note_unicode_emoji_round_trip(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    note = "eSIM za mamu ❤️"
    response = _patch_note(
        client, access=access, esim_id=alice_esim.pk, body={"note": note}
    )
    assert response.status_code == 200
    assert response.json()["note"] == note
    alice_esim.refresh_from_db()
    assert alice_esim.note == note


@pytest.mark.django_db
def test_patch_note_xss_plain_text_round_trip(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    note = "<script>alert(1)</script>"
    response = _patch_note(
        client, access=access, esim_id=alice_esim.pk, body={"note": note}
    )
    assert response.status_code == 200
    assert response.json()["note"] == note
    alice_esim.refresh_from_db()
    assert alice_esim.note == note


@pytest.mark.django_db
def test_patch_note_does_not_mutate_other_fields(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    before = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    ).json()

    response = _patch_note(
        client,
        access=access,
        esim_id=alice_esim.pk,
        body={
            "note": "Japan trip",
            "iccid": "891999999999999999",
            "status": Esim.Status.ACTIVATED,
            "package_title": "hijacked",
            "activation_policy": "installation",
        },
    )
    assert response.status_code == 200
    after = response.json()
    assert after["note"] == "Japan trip"
    for key in (
        "iccid",
        "status",
        "package_title",
        "activation_policy",
        "lpa",
        "matching_id",
        "qrcode",
    ):
        assert after[key] == before[key], key

    alice_esim.refresh_from_db()
    assert alice_esim.iccid == before["iccid"]
    assert alice_esim.status == before["status"]
    assert alice_esim.activation_policy == before["activation_policy"]


@pytest.mark.django_db
def test_patch_empty_body_is_noop(client: Client, user: User, alice_esim: Esim) -> None:
    access = _access_token(client, user.email)
    alice_esim.note = "keep me"
    alice_esim.save(update_fields=["note"])

    before = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    ).json()

    response = _patch_note(client, access=access, esim_id=alice_esim.pk, body={})
    assert response.status_code == 200
    after = response.json()
    assert after["note"] == "keep me"
    for key, value in before.items():
        if key == "updated_at":
            continue
        assert after[key] == value, key
