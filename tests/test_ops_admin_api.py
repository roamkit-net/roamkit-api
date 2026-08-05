"""Tests for staff read-only /api/v1/admin/* Operations Dashboard."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.billing.models import DepositRequest, LedgerReferenceType
from apps.billing.services.credit import credit_service
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="member@example.com", password=PASSWORD)


@pytest.fixture
def staff_user(db) -> User:
    return User.objects.create_user(
        email="ops@example.com",
        password=PASSWORD,
        is_staff=True,
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-ops-1gb",
        title="Ops Pack 1GB",
        operator_title="TestOp",
        country_code="HR",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("9.99"),
        net_price_usd=Decimal("4.00"),
        synced_at=timezone.now(),
    )


def _access_token(client: Client, email: str) -> str:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()["access"]


def _auth(client: Client, person: User) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {_access_token(client, person.email)}"}


def _make_esim(*, user: User, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-6:]}",
        **product_snapshot_kwargs(package),
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid=iccid,
        status=Esim.Status.INSTALLED,
    )


@pytest.mark.django_db
def test_ops_dashboard_requires_auth(client: Client) -> None:
    assert client.get("/api/v1/admin/dashboard/").status_code == 401


@pytest.mark.django_db
def test_ops_dashboard_requires_staff(client: Client, user: User) -> None:
    response = client.get("/api/v1/admin/dashboard/", **_auth(client, user))
    assert response.status_code == 403


@pytest.mark.django_db
def test_ops_dashboard_staff_ok(client: Client, staff_user: User, user: User) -> None:
    response = client.get("/api/v1/admin/dashboard/", **_auth(client, staff_user))
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["schema_version"] == 1
    assert "kpi" in payload
    assert "pending_work" in payload
    assert "financial" in payload
    assert "activity" in payload
    assert "health" in payload
    health = payload["health"]
    assert health["schema_version"] == 1
    assert "overall_status" in health
    assert health["dependencies"]["database"]["status"] in {
        "healthy",
        "unhealthy",
        "degraded",
        "unknown",
    }
    assert "whatsapp" not in health.get("checks", {})
    assert "whatsapp" not in health.get("providers", {})
    assert payload["kpi"]["users_total"] >= 2


@pytest.mark.django_db
def test_ops_search_groups_and_hits(
    client: Client,
    staff_user: User,
    user: User,
    package: Package,
) -> None:
    esim = _make_esim(user=user, package=package, iccid="891000000000009999")
    DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("10.000000"),
        payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        tx_hash="0x" + ("cd" * 32),
        idempotency_key="ops-search-dep-1",
        status=DepositRequest.Status.PENDING,
    )

    response = client.get(
        "/api/v1/admin/search/",
        {"q": "member@"},
        **_auth(client, staff_user),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert set(body.keys()) >= {
        "users",
        "orders",
        "deposits",
        "esims",
        "vouchers",
        "query",
    }
    assert any(u["id"] == user.pk for u in body["users"])

    by_iccid = client.get(
        "/api/v1/admin/search/",
        {"q": esim.iccid},
        **_auth(client, staff_user),
    ).json()
    assert any(e["label"] == esim.iccid for e in by_iccid["esims"])

    by_tx = client.get(
        "/api/v1/admin/search/",
        {"q": "0xcdcd"},
        **_auth(client, staff_user),
    ).json()
    assert len(by_tx["deposits"]) >= 1


@pytest.mark.django_db
def test_ops_users_list_paginated_and_badges(
    client: Client,
    staff_user: User,
    user: User,
) -> None:
    user.google_sub = "google-sub-1"
    user.save(update_fields=["google_sub"])

    response = client.get("/api/v1/admin/users/", **_auth(client, staff_user))
    assert response.status_code == 200
    body = response.json()
    assert "results" in body
    assert "count" in body
    member = next(r for r in body["results"] if r["id"] == user.pk)
    assert "google" in member["badges"]
    assert member["balance"] is not None


@pytest.mark.django_db
def test_ops_user_detail_timeline(
    client: Client,
    staff_user: User,
    user: User,
    package: Package,
) -> None:
    _make_esim(user=user, package=package, iccid="891000000000008888")
    credit_service.credit(
        user.billing_account,
        Decimal("5.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="ops-timeline-adj",
        idempotency_key="ops-timeline-adj",
    )

    response = client.get(
        f"/api/v1/admin/users/{user.pk}/",
        **_auth(client, staff_user),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["email"] == user.email
    assert isinstance(body["timeline"], list)
    assert len(body["timeline"]) >= 1
    event = body["timeline"][0]
    assert set(event.keys()) >= {
        "schema_version",
        "type",
        "timestamp",
        "title",
        "subtitle",
        "reference_id",
        "severity",
        "event_group",
        "icon",
    }
    assert any(e["type"] == "user.created" for e in body["timeline"])
    assert any(e["event_group"] == "billing" for e in body["timeline"])
    assert "qrcode" not in body
    assert "raw_rpc_response" not in json.dumps(body)


@pytest.mark.django_db
def test_ops_orders_and_deposits_paginated(
    client: Client,
    staff_user: User,
    user: User,
    package: Package,
) -> None:
    _make_esim(user=user, package=package, iccid="891000000000007777")
    DepositRequest.objects.create(
        account=user.billing_account,
        amount_requested=Decimal("3.000000"),
        payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        idempotency_key="ops-dep-list-1",
        status=DepositRequest.Status.PENDING,
    )

    orders = client.get("/api/v1/admin/orders/", **_auth(client, staff_user))
    assert orders.status_code == 200
    assert "results" in orders.json()

    deposits = client.get("/api/v1/admin/deposits/", **_auth(client, staff_user))
    assert deposits.status_code == 200
    assert deposits["Cache-Control"] == "no-store"
    assert "results" in deposits.json()


@pytest.mark.django_db
def test_me_exposes_is_staff(client: Client, staff_user: User, user: User) -> None:
    staff_me = client.get("/api/v1/auth/me/", **_auth(client, staff_user)).json()
    assert staff_me["is_staff"] is True
    member_me = client.get("/api/v1/auth/me/", **_auth(client, user)).json()
    assert member_me["is_staff"] is False


_OPS_PATHS = (
    "/api/v1/admin/dashboard/",
    "/api/v1/admin/search/?q=member",
    "/api/v1/admin/users/",
    "/api/v1/admin/orders/",
    "/api/v1/admin/deposits/",
)


@pytest.mark.django_db
def test_all_ops_endpoints_no_store_and_auth_matrix(
    client: Client,
    staff_user: User,
    user: User,
) -> None:
    for path in _OPS_PATHS:
        assert client.get(path).status_code == 401
        assert client.get(path, **_auth(client, user)).status_code == 403
        ok = client.get(path, **_auth(client, staff_user))
        assert ok.status_code == 200, path
        assert ok["Cache-Control"] == "no-store", path

    detail = f"/api/v1/admin/users/{user.pk}/"
    assert client.get(detail).status_code == 401
    assert client.get(detail, **_auth(client, user)).status_code == 403
    staff_detail = client.get(detail, **_auth(client, staff_user))
    assert staff_detail.status_code == 200
    assert staff_detail["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_dashboard_and_detail_query_counts_bounded(
    client: Client,
    staff_user: User,
    user: User,
    package: Package,
) -> None:
    """Aggregations must not explode with more members (no per-row N+1)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for i in range(8):
        other = User.objects.create_user(
            email=f"bulk{i}@example.com",
            password=PASSWORD,
        )
        _make_esim(
            user=other,
            package=package,
            iccid=f"89100000000000{i:04d}",
        )
        credit_service.credit(
            other.billing_account,
            Decimal("1.000000"),
            reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
            reference_id=f"ops-bulk-{i}",
            idempotency_key=f"ops-bulk-{i}",
        )

    headers = _auth(client, staff_user)
    with CaptureQueriesContext(connection) as dash_ctx:
        assert client.get("/api/v1/admin/dashboard/", **headers).status_code == 200
    with CaptureQueriesContext(connection) as detail_ctx:
        assert (
            client.get(f"/api/v1/admin/users/{user.pk}/", **headers).status_code == 200
        )

    # Fixed-shape aggregates + capped activity/timeline fetches.
    assert len(dash_ctx) < 45, f"dashboard queries={len(dash_ctx)}"
    assert len(detail_ctx) < 35, f"detail queries={len(detail_ctx)}"
