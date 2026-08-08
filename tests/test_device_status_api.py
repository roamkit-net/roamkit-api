"""Org-authenticated UEM device status API (ADR 020 / PR17)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.orders.models import Order
from apps.organizations.models import (
    DeviceBindingStatus,
    Membership,
    MembershipRole,
    MembershipStatus,
)
from apps.organizations.services import create_device_binding, create_organization

STATUS_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "apps"
    / "organizations"
    / "services"
    / "device_status.py"
)

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password=PASSWORD)


@pytest.fixture
def member_user(db):
    return User.objects.create_user(email="member@example.com", password=PASSWORD)


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(email="viewer@example.com", password=PASSWORD)


@pytest.fixture
def stranger(db):
    return User.objects.create_user(email="stranger@example.com", password=PASSWORD)


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
def org(owner):
    return create_organization(name="Fleet Ops", actor=owner)


def _access_token(client: Client, email: str) -> str:
    resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["access"]


def _auth(client, user):
    return {"HTTP_AUTHORIZATION": f"Bearer {_access_token(client, user.email)}"}


def _add_member(org, user, role: str) -> Membership:
    return Membership.objects.create(
        organization=org,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


def _make_esim(*, account, user, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-4:]}",
        customer_ref=f"ref-{iccid[-4:]}",
    )
    return Esim.objects.create(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        status=Esim.Status.INSTALLED,
    )


def _status_url(org_id, device_external_id: str) -> str:
    return f"/api/v1/orgs/{org_id}/devices/{device_external_id}/status/"


@pytest.mark.django_db
def test_member_can_read_status_from_cache(client, owner, member_user, org, package):
    _add_member(org, member_user, MembershipRole.MEMBER)
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000011111",
    )
    esim.usage_remaining_mb = 512
    esim.usage_total_mb = 1024
    esim.usage_is_unlimited = False
    esim.usage_expired_at = timezone.now()
    esim.usage_synced_at = timezone.now()
    esim.save()
    EsimAutoTopupPolicy.objects.create(
        account=org.account,
        esim=esim,
        package_id="topup-1gb",
        enabled=True,
        status=EsimAutoTopupPolicy.Status.ACTIVE,
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=100,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)

    source = STATUS_SERVICE.read_text(encoding="utf-8")
    assert "get_topup_provider" not in source
    assert "UsageService" not in source

    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, member_user),
    )
    assert resp.status_code == 200, resp.content
    payload = resp.json()
    assert payload["device_external_id"] == binding.device_external_id
    assert payload["binding_status"] == DeviceBindingStatus.ACTIVE
    assert payload["esim"]["id"] == esim.pk
    assert payload["esim"]["iccid"] == esim.iccid
    assert payload["esim"]["status"] == Esim.Status.INSTALLED
    assert payload["usage"]["data_remaining"] == "512 MB"
    assert payload["usage"]["data_used"] == "512 MB"
    assert payload["usage"]["expires_at"] is not None
    assert payload["auto_topup"]["enabled"] is True
    assert payload["checked_at"]


@pytest.mark.django_db
def test_unknown_usage_returns_nulls_not_error(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000022222",
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, owner),
    )
    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert usage["data_remaining"] is None
    assert usage["data_used"] is None
    assert usage["expires_at"] is None
    assert resp.json()["auto_topup"]["enabled"] is False


@pytest.mark.django_db
def test_viewer_can_read_status(client, owner, viewer_user, org, package):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000033333",
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, viewer_user),
    )
    assert resp.status_code == 200


@pytest.mark.django_db
def test_unbound_binding_not_found(client, owner, org, package):
    from apps.organizations.services import unbind_device_binding

    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000044444",
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    device_id = binding.device_external_id
    unbind_device_binding(owner, org.pk, binding.pk)
    resp = client.get(_status_url(org.pk, device_id), **_auth(client, owner))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_replaced_binding_device_id_not_found(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000055555",
    )
    first = create_device_binding(owner, org.pk, esim_id=esim.pk)
    old_device = first.device_external_id
    create_device_binding(owner, org.pk, esim_id=esim.pk, replace=True)
    resp = client.get(_status_url(org.pk, old_device), **_auth(client, owner))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_cross_org_lookup_not_found(client, owner, stranger, package):
    org_a = create_organization(name="A", actor=owner)
    org_b = create_organization(name="B", actor=stranger)
    esim = _make_esim(
        account=org_b.account,
        user=stranger,
        package=package,
        iccid="891000000000066666",
    )
    binding = create_device_binding(stranger, org_b.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org_a.pk, binding.device_external_id),
        **_auth(client, owner),
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_stranger_without_membership_not_found(client, owner, stranger, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000077777",
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, stranger),
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_suspended_membership_forbidden(client, owner, member_user, org, package):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status", "updated_at"])
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000088888",
    )
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, member_user),
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_unlimited_usage_signal(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000099999",
    )
    esim.usage_is_unlimited = True
    esim.usage_remaining_mb = None
    esim.usage_total_mb = None
    esim.save()
    binding = create_device_binding(owner, org.pk, esim_id=esim.pk)
    resp = client.get(
        _status_url(org.pk, binding.device_external_id),
        **_auth(client, owner),
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["data_remaining"] == "unlimited"
