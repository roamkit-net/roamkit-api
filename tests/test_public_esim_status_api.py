"""Public Matching ID status (ADR 022)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.esims.services.public_status import mask_iccid, redact_matching_id
from apps.orders.models import Order

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/public/esim/status/"
MATCHING_ID = "TN2026060518450826EE68B1"
ICCID = "89445012345678901234"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="status@example.com", password=PASSWORD)


@pytest.fixture
def account(user):
    return ensure_billing_account(user)


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-eu-5gb-30d",
        title="Europe 5GB",
        operator_title="Op",
        country_code="HR",
        data_allowance="5 GB",
        validity_days=30,
        price_usd=Decimal("15.00"),
        synced_at=timezone.now(),
        is_active=True,
    )


def _make_esim(
    *,
    account,
    user,
    package: Package,
    matching_id: str = MATCHING_ID,
    iccid: str = ICCID,
    archived: bool = False,
    with_usage: bool = True,
    with_coverage: bool = True,
) -> Esim:
    coverage = None
    if with_coverage:
        coverage = [
            {
                "country_code": "HR",
                "country_name": "Croatia",
                "operators": ["Operator A"],
            }
        ]
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-4:]}",
        package_title=package.title,
        location_title="Europe",
        country_code="HR",
        coverage_type="regional",
        coverage_snapshot=coverage,
        data_allowance=package.data_allowance,
        validity_days=package.validity_days,
    )
    esim = Esim(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        matching_id=matching_id,
        status=Esim.Status.IN_USE,
    )
    if with_usage:
        esim.usage_remaining_mb = 1200
        esim.usage_total_mb = 2000
        esim.usage_expired_at = timezone.now()
        esim.usage_synced_at = timezone.now()
    if archived:
        esim.archived_at = timezone.now()
    esim.save()
    return esim


def _post(client: Client, payload, **extra):
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload)
    else:
        body = payload
    return client.post(
        URL,
        data=body,
        content_type="application/json",
        **extra,
    )


@pytest.mark.django_db
def test_status_success_cache_snapshot(client, account, user, package):
    _make_esim(account=account, user=user, package=package)
    EsimAutoTopupPolicy.objects.create(
        account=account,
        esim=Esim.objects.get(matching_id=MATCHING_ID),
        package_id="pkg-eu-5gb-30d",
        enabled=True,
        status=EsimAutoTopupPolicy.Status.ACTIVE,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )

    resp = _post(client, {"matching_id": f"  {MATCHING_ID}  "})
    assert resp.status_code == 200, resp.content
    payload = resp.json()
    assert payload["esim"]["iccid"] == mask_iccid(ICCID)
    assert payload["esim"]["status"] == Esim.Status.IN_USE
    assert "id" not in payload["esim"]
    assert payload["usage"]["data_remaining"] == "1200 MB"
    assert payload["usage"]["data_used"] == "800 MB"
    assert payload["usage"]["expires_at"] is not None
    assert payload["usage"]["synced_at"] is not None
    assert payload["auto_topup"] == {"enabled": True}
    assert payload["plan"]["title"] == "Europe 5GB"
    assert payload["plan"]["data_allowance"] == "5 GB"
    assert payload["packages"] is None
    assert payload["coverage"]["coverage_type"] == "regional"
    assert payload["coverage"]["coverage"][0]["country_code"] == "HR"
    assert "device_external_id" not in payload
    assert "binding_status" not in payload
    assert MATCHING_ID not in resp.content.decode()
    assert ICCID not in resp.content.decode()
    assert "lpa" not in payload


@pytest.mark.django_db
def test_status_usage_null_when_cache_missing(client, account, user, package):
    _make_esim(account=account, user=user, package=package, with_usage=False)
    resp = _post(client, {"matching_id": MATCHING_ID})
    assert resp.status_code == 200
    assert resp.json()["usage"] is None


@pytest.mark.django_db
def test_status_coverage_null_when_snapshot_missing(client, account, user, package):
    _make_esim(account=account, user=user, package=package, with_coverage=False)
    resp = _post(client, {"matching_id": MATCHING_ID})
    assert resp.status_code == 200
    assert resp.json()["coverage"] is None


@pytest.mark.django_db
def test_status_auto_topup_false_without_active_policy(client, account, user, package):
    esim = _make_esim(account=account, user=user, package=package)
    EsimAutoTopupPolicy.objects.create(
        account=account,
        esim=esim,
        package_id="pkg-eu-5gb-30d",
        enabled=True,
        status=EsimAutoTopupPolicy.Status.PAUSED,
        reason=EsimAutoTopupPolicy.Reason.MANUAL_PAUSE,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    resp = _post(client, {"matching_id": MATCHING_ID})
    assert resp.status_code == 200
    assert resp.json()["auto_topup"] == {"enabled": False}


@pytest.mark.django_db
def test_jwt_header_does_not_widen_or_reject(client, account, user, package):
    _make_esim(account=account, user=user, package=package)
    resp = _post(
        client,
        {"matching_id": MATCHING_ID},
        HTTP_AUTHORIZATION="Bearer not-a-real-token",
    )
    assert resp.status_code == 200
    assert resp.json()["packages"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"matching_id": ""},
        {"matching_id": "   "},
        {"matching_id": "X" * 129},
        {"matching_id": "UNKNOWNTOKEN"},
    ],
)
@pytest.mark.django_db
def test_matching_id_miss_is_generic_404(client, payload):
    resp = _post(client, payload)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "matching_id_not_found"
    assert body["detail"] == "Matching ID not found."


@pytest.mark.django_db
def test_archived_esim_is_not_found(client, account, user, package):
    _make_esim(account=account, user=user, package=package, archived=True)
    resp = _post(client, {"matching_id": MATCHING_ID})
    assert resp.status_code == 404
    assert resp.json()["code"] == "matching_id_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"matching_id": MATCHING_ID, "iccid": ICCID},
        {"matching_id": MATCHING_ID, "device_serial": "ABC"},
        {"matching_id": MATCHING_ID, "lpa": "LPA:1$x$y"},
        {"matching_id": MATCHING_ID, "extra": "nope"},
        ["matching_id"],
    ],
)
@pytest.mark.django_db
def test_extra_or_non_object_body_is_400(client, payload):
    resp = _post(client, payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


@pytest.mark.django_db
def test_malformed_json_is_400(client):
    resp = _post(client, "{not-json")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


@pytest.mark.django_db
def test_non_string_matching_id_is_400(client):
    resp = _post(client, {"matching_id": 123})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


@pytest.mark.django_db
def test_save_persists_trimmed_matching_id(account, user, package):
    esim = _make_esim(
        account=account,
        user=user,
        package=package,
        matching_id="  TNTRIMTEST  ",
        iccid="89445000000000000001",
    )
    esim.refresh_from_db()
    assert esim.matching_id == "TNTRIMTEST"


@pytest.mark.django_db
def test_unique_constraint_rejects_duplicate_matching_id(account, user, package):
    _make_esim(account=account, user=user, package=package)
    with pytest.raises(IntegrityError):
        _make_esim(
            account=account,
            user=user,
            package=package,
            iccid="89445000000000000002",
        )


@pytest.mark.django_db
def test_normalize_migration_aborts_on_trimmed_duplicates(account, user, package):
    first = _make_esim(account=account, user=user, package=package)
    second = _make_esim(
        account=account,
        user=user,
        package=package,
        matching_id="OTHER",
        iccid="89445000000000000003",
    )
    Esim.objects.filter(pk=first.pk).update(matching_id=" DUP ")
    Esim.objects.filter(pk=second.pk).update(matching_id="DUP")
    from django.db.models import Count
    from django.db.models.functions import Trim

    collisions = list(
        Esim.objects.exclude(matching_id="")
        .annotate(trimmed=Trim("matching_id"))
        .exclude(trimmed="")
        .values("trimmed")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    assert len(collisions) == 1


def test_public_status_module_is_cache_only() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "apps"
        / "esims"
        / "services"
        / "public_status.py"
    ).read_text(encoding="utf-8")
    assert "UsageService" not in source
    assert "PackageHistoryService" not in source
    assert "get_topup_provider" not in source
    assert "get_sim_package_provider" not in source


def test_mask_and_redact_helpers() -> None:
    assert mask_iccid(ICCID) == "894450••••••1234"
    assert mask_iccid("123") == "••••"
    assert redact_matching_id(MATCHING_ID) == "TN••••B1"
    assert MATCHING_ID not in redact_matching_id(MATCHING_ID)
