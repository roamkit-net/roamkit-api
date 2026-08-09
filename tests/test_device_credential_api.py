"""Device credential + device-facing status API (ADR 020 / PR18)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.models import (
    DeviceBinding,
    DeviceBindingEvent,
    DeviceBindingEventAction,
    Membership,
    MembershipRole,
    MembershipStatus,
)
from apps.organizations.services import (
    create_device_binding,
    create_organization,
    unbind_device_binding,
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


def _device_status(client, *, device_external_id: str, credential: str, **extra):
    body = {
        "device_external_id": device_external_id,
        "credential": credential,
        **extra,
    }
    return client.post(
        "/api/v1/device/status/",
        data=json.dumps(body),
        content_type="application/json",
    )


@pytest.mark.django_db
def test_create_issues_credential_once_and_device_status_works(
    client, owner, org, package
):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000111111",
    )
    create = client.post(
        f"/api/v1/orgs/{org.pk}/device-bindings/",
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    assert create.status_code == 201, create.content
    payload = create.json()
    credential = payload["credential"]
    device_id = payload["binding"]["device_external_id"]
    binding_id = payload["binding"]["id"]

    binding = DeviceBinding.objects.get(pk=binding_id)
    assert binding.credential_hash
    assert credential not in binding.credential_hash
    assert DeviceBindingEvent.objects.filter(
        binding_id=binding_id, action=DeviceBindingEventAction.CREDENTIAL_ISSUE
    ).exists()

    # List/detail must not echo plaintext credential.
    listed = client.get(
        f"/api/v1/orgs/{org.pk}/device-bindings/", **_auth(client, owner)
    )
    assert "credential" not in json.dumps(listed.json())

    resp = _device_status(client, device_external_id=device_id, credential=credential)
    assert resp.status_code == 200, resp.content
    assert resp.json()["device_external_id"] == device_id
    assert resp.json()["esim"]["id"] == esim.pk


@pytest.mark.django_db
def test_wrong_credential_and_device_id_only_are_404(client, owner, org, package):
    result = create_device_binding(
        owner,
        org.pk,
        esim_id=_make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid="891000000000222222",
        ).pk,
    )
    assert (
        _device_status(
            client,
            device_external_id=result.binding.device_external_id,
            credential="wrong-secret",
        ).status_code
        == 404
    )
    # Blank credential fails validation before auth lookup (not a credential oracle).
    assert (
        _device_status(
            client,
            device_external_id=result.binding.device_external_id,
            credential="",
        ).status_code
        == 400
    )
    # Unknown device_external_id with any credential is 404:
    assert (
        _device_status(
            client,
            device_external_id="00000000-0000-0000-0000-000000000000",
            credential=result.credential,
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_rotate_invalidates_old_credential(client, owner, org, package):
    result = create_device_binding(
        owner,
        org.pk,
        esim_id=_make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid="891000000000333333",
        ).pk,
    )
    old = result.credential
    device_id = result.binding.device_external_id
    rotate = client.post(
        f"/api/v1/orgs/{org.pk}/device-bindings/{result.binding.pk}/credential/rotate/",
        **_auth(client, owner),
    )
    assert rotate.status_code == 200, rotate.content
    new = rotate.json()["credential"]
    assert new != old
    assert DeviceBindingEvent.objects.filter(
        binding_id=result.binding.pk,
        action=DeviceBindingEventAction.CREDENTIAL_ROTATE,
    ).exists()

    assert (
        _device_status(client, device_external_id=device_id, credential=old).status_code
        == 404
    )
    assert (
        _device_status(client, device_external_id=device_id, credential=new).status_code
        == 200
    )


@pytest.mark.django_db
def test_unbind_and_replaced_reject_device_status(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000444444",
    )
    first = create_device_binding(owner, org.pk, esim_id=esim.pk)
    old_cred = first.credential
    old_device = first.binding.device_external_id

    second = create_device_binding(owner, org.pk, esim_id=esim.pk, replace=True)
    assert (
        _device_status(
            client, device_external_id=old_device, credential=old_cred
        ).status_code
        == 404
    )
    assert (
        _device_status(
            client,
            device_external_id=second.binding.device_external_id,
            credential=second.credential,
        ).status_code
        == 200
    )

    unbind_device_binding(owner, org.pk, second.binding.pk)
    assert (
        _device_status(
            client,
            device_external_id=second.binding.device_external_id,
            credential=second.credential,
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_member_cannot_rotate_credential(client, owner, member_user, org, package):
    Membership.objects.create(
        organization=org,
        user=member_user,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )
    result = create_device_binding(
        owner,
        org.pk,
        esim_id=_make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid="891000000000555555",
        ).pk,
    )
    resp = client.post(
        f"/api/v1/orgs/{org.pk}/device-bindings/{result.binding.pk}/credential/rotate/",
        **_auth(client, member_user),
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_device_status_rejects_organization_id_field(client, owner, org, package):
    result = create_device_binding(
        owner,
        org.pk,
        esim_id=_make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid="891000000000666666",
        ).pk,
    )
    resp = _device_status(
        client,
        device_external_id=result.binding.device_external_id,
        credential=result.credential,
        organization_id=str(org.pk),
    )
    assert resp.status_code == 400
    assert "organization_id" in resp.json()


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "rest_framework_simplejwt.authentication.JWTAuthentication"
        ],
        "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
        "DEFAULT_THROTTLE_RATES": {"device_status": "2/hour"},
        "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    }
)
def test_device_status_is_rate_limited(client, owner, org, package):
    cache.clear()
    result = create_device_binding(
        owner,
        org.pk,
        esim_id=_make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid="891000000000777777",
        ).pk,
    )
    kwargs = {
        "device_external_id": result.binding.device_external_id,
        "credential": result.credential,
    }
    assert _device_status(client, **kwargs).status_code == 200
    assert _device_status(client, **kwargs).status_code == 200
    assert _device_status(client, **kwargs).status_code == 429
