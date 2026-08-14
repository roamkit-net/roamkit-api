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
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from shared.providers.esim import SimPackageDTO, TopupPackage, UsageDTO

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


class FakeSimPackageProvider:
    def __init__(self, rows: list[SimPackageDTO] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[str] = []

    def list_sim_packages(self, iccid: str) -> list[SimPackageDTO]:
        self.calls.append(iccid)
        return self.rows


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
        account=user.billing_account,
        order=order,
        iccid=iccid,
        lpa="lpa.airalo.com",
        matching_id=f"TEST-{iccid}",
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
    assert item["archived_at"] is None
    assert item["auto_topup"] is None
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
    assert payload["auto_topup"] is None
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
    assert payload["results"][0]["list_price_usd"] == "5.00"
    assert payload["results"][0]["discount_percent"] == "0.00"
    assert payload["results"][0]["pricing_reason"] == "retail"
    assert "net_price_usd" not in payload["results"][0]
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


def _history_row(
    *,
    instance_id: str = "728",
    status: str = "active",
    plan_type: str = "topup",
    package_external_id: str = "topup-1gb",
    is_unlimited: bool = False,
    remaining_mb: int | None = 900,
) -> SimPackageDTO:
    return SimPackageDTO(
        instance_id=instance_id,
        status=status,
        remaining_mb=remaining_mb,
        activated_at="2026-08-12T10:50:00+00:00",
        expired_at="2026-08-19T10:50:00+00:00",
        finished_at=None,
        package_external_id=package_external_id,
        plan_type=plan_type,
        data_allowance="Unlimited" if is_unlimited else "1 GB",
        validity_days=7,
        is_unlimited=is_unlimited,
        provider_order_id=None,
    )


@pytest.mark.django_db
def test_packages_lists_applied_history(
    client: Client,
    user: User,
    alice_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeSimPackageProvider(
        [
            _history_row(instance_id="1", plan_type="sim", status="expired"),
            _history_row(instance_id="2", status="not_active"),
            _history_row(instance_id="3", status="unknown"),
        ]
    )
    monkeypatch.setattr(
        "apps.esims.views.get_sim_package_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/packages/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert [row["id"] for row in payload["results"]] == ["1", "2", "3"]
    first = payload["results"][0]
    assert first["kind"] == "esim"
    assert first["status"] == "expired"
    assert first["paid_usd"] == "11.50"
    assert first["currency"] == "USD"
    assert "net_price" not in first
    assert "net_price_usd" not in first
    assert payload["results"][1]["status"] == "not_active"
    assert payload["results"][2]["status"] == "unknown"
    assert provider.calls == [alice_esim.iccid]


@pytest.mark.django_db
def test_packages_unlimited_remaining_is_null(
    client: Client,
    user: User,
    alice_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeSimPackageProvider([_history_row(is_unlimited=True, remaining_mb=0)])
    monkeypatch.setattr(
        "apps.esims.views.get_sim_package_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)
    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/packages/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["is_unlimited"] is True
    assert row["remaining_mb"] is None


@pytest.mark.django_db
def test_packages_hides_other_users_esim(
    client: Client,
    user: User,
    bob_esim: Esim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeSimPackageProvider([_history_row()])
    monkeypatch.setattr(
        "apps.esims.views.get_sim_package_provider",
        lambda: provider,
    )
    access = _access_token(client, user.email)

    response = client.get(
        f"/api/v1/me/esims/{bob_esim.pk}/packages/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 404
    assert provider.calls == []


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
            "archived_at": "2026-01-01T00:00:00Z",
            "auto_topup": {
                "enabled": True,
                "status": "active",
                "reason": "",
            },
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
        "archived_at",
        "auto_topup",
    ):
        assert after[key] == before[key], key

    alice_esim.refresh_from_db()
    assert alice_esim.iccid == before["iccid"]
    assert alice_esim.status == before["status"]
    assert alice_esim.activation_policy == before["activation_policy"]
    assert alice_esim.archived_at is None


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


def _post_archive(client: Client, *, access: str, esim_id: int) -> Any:
    return client.post(
        f"/api/v1/me/esims/{esim_id}/archive/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


def _post_unarchive(client: Client, *, access: str, esim_id: int) -> Any:
    return client.post(
        f"/api/v1/me/esims/{esim_id}/unarchive/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )


@pytest.mark.django_db
def test_list_excludes_archived_by_default(
    client: Client, user: User, alice_esim: Esim, package: Package
) -> None:
    archived = _make_esim(user=user, package=package, iccid="891000000000009200")
    archived.archived_at = timezone.now()
    archived.save(update_fields=["archived_at"])

    access = _access_token(client, user.email)
    response = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    payload = response.json()
    ids = {row["id"] for row in payload["results"]}
    assert alice_esim.pk in ids
    assert archived.pk not in ids


@pytest.mark.django_db
def test_list_include_archived_true(
    client: Client, user: User, alice_esim: Esim, package: Package
) -> None:
    archived = _make_esim(user=user, package=package, iccid="891000000000009201")
    archived.archived_at = timezone.now()
    archived.save(update_fields=["archived_at"])

    access = _access_token(client, user.email)
    response = client.get(
        "/api/v1/me/esims/?include_archived=true",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    payload = response.json()
    by_id = {row["id"]: row for row in payload["results"]}
    assert alice_esim.pk in by_id
    assert archived.pk in by_id
    assert by_id[alice_esim.pk]["archived_at"] is None
    assert by_id[archived.pk]["archived_at"] is not None


@pytest.mark.django_db
def test_archive_unarchive_round_trip(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    status_before = alice_esim.status

    archived = _post_archive(client, access=access, esim_id=alice_esim.pk)
    assert archived.status_code == 200
    body = archived.json()
    assert body["id"] == alice_esim.pk
    assert body["archived_at"] is not None
    assert body["status"] == status_before
    assert "iccid" in body

    alice_esim.refresh_from_db()
    assert alice_esim.archived_at is not None
    assert alice_esim.status == status_before

    restored = _post_unarchive(client, access=access, esim_id=alice_esim.pk)
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    alice_esim.refresh_from_db()
    assert alice_esim.archived_at is None


@pytest.mark.django_db
def test_archive_unarchive_idempotent_200(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)

    first = _post_archive(client, access=access, esim_id=alice_esim.pk)
    assert first.status_code == 200
    first_at = first.json()["archived_at"]

    second = _post_archive(client, access=access, esim_id=alice_esim.pk)
    assert second.status_code == 200
    assert second.json()["archived_at"] == first_at

    cleared = _post_unarchive(client, access=access, esim_id=alice_esim.pk)
    assert cleared.status_code == 200
    assert cleared.json()["archived_at"] is None

    again = _post_unarchive(client, access=access, esim_id=alice_esim.pk)
    assert again.status_code == 200
    assert again.json()["archived_at"] is None


@pytest.mark.django_db
def test_archive_hides_other_users_esim(
    client: Client, user: User, bob_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = _post_archive(client, access=access, esim_id=bob_esim.pk)
    assert response.status_code == 404
    bob_esim.refresh_from_db()
    assert bob_esim.archived_at is None


@pytest.mark.django_db
def test_unarchive_hides_other_users_esim(
    client: Client, user: User, bob_esim: Esim
) -> None:
    bob_esim.archived_at = timezone.now()
    bob_esim.save(update_fields=["archived_at"])
    access = _access_token(client, user.email)
    response = _post_unarchive(client, access=access, esim_id=bob_esim.pk)
    assert response.status_code == 404
    bob_esim.refresh_from_db()
    assert bob_esim.archived_at is not None


@pytest.mark.django_db
def test_detail_returns_archived_esim(
    client: Client, user: User, alice_esim: Esim
) -> None:
    alice_esim.archived_at = timezone.now()
    alice_esim.save(update_fields=["archived_at"])
    access = _access_token(client, user.email)
    response = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    assert response.json()["archived_at"] is not None


def _paused_funds_policy(*, user: User, esim: Esim) -> EsimAutoTopupPolicy:
    return EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-1gb",
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        enabled=True,
        status=EsimAutoTopupPolicy.Status.PAUSED,
        reason=EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS,
    )


@pytest.mark.django_db
def test_list_auto_topup_snapshot_null_without_policy(
    client: Client, user: User, alice_esim: Esim
) -> None:
    access = _access_token(client, user.email)
    response = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["id"] == alice_esim.pk
    assert item["auto_topup"] is None


@pytest.mark.django_db
def test_list_and_detail_auto_topup_snapshot_paused_funds(
    client: Client, user: User, alice_esim: Esim
) -> None:
    _paused_funds_policy(user=user, esim=alice_esim)
    access = _access_token(client, user.email)

    listed = client.get(
        "/api/v1/me/esims/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert listed.status_code == 200
    snap = listed.json()["results"][0]["auto_topup"]
    assert snap == {
        "enabled": True,
        "status": EsimAutoTopupPolicy.Status.PAUSED,
        "reason": EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS,
    }
    assert set(snap) == {"enabled", "status", "reason"}

    detail = client.get(
        f"/api/v1/me/esims/{alice_esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert detail.status_code == 200
    assert detail.json()["auto_topup"] == snap


@pytest.mark.django_db
def test_list_auto_topup_snapshot_present_when_archived(
    client: Client, user: User, alice_esim: Esim
) -> None:
    """API still returns the snapshot; Action required filtering is the client."""
    _paused_funds_policy(user=user, esim=alice_esim)
    alice_esim.archived_at = timezone.now()
    alice_esim.save(update_fields=["archived_at"])
    access = _access_token(client, user.email)
    response = client.get(
        "/api/v1/me/esims/?include_archived=true",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )
    assert response.status_code == 200
    item = next(row for row in response.json()["results"] if row["id"] == alice_esim.pk)
    assert item["archived_at"] is not None
    assert item["auto_topup"]["reason"] == (
        EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
    )


def test_openapi_esim_has_auto_topup_snapshot() -> None:
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    schemas = doc["components"]["schemas"]
    esim_props = schemas["Esim"]["properties"]
    assert "auto_topup" in esim_props
    assert esim_props["auto_topup"].get("nullable") is True
    snap = schemas["EsimAutoTopupSnapshot"]["properties"]
    assert set(snap) == {"enabled", "status", "reason"}
    assert "package_id" not in snap
    assert "net_price_usd" not in snap
    assert "net_price" not in snap


def test_openapi_esim_packages_list_has_applied_package() -> None:
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert "esim_packages_list" in {
        op.get("operationId")
        for methods in (doc.get("paths") or {}).values()
        if isinstance(methods, dict)
        for op in methods.values()
        if isinstance(op, dict)
    }
    props = doc["components"]["schemas"]["AppliedPackage"]["properties"]
    assert "paid_usd" in props
    assert "net_price" not in props
    assert "net_price_usd" not in props
    assert "/api/v1/me/esims/{id}/packages/" in doc["paths"]
