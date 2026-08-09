"""DeviceBinding schema + HTTP API (ADR 020 / PR16)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.models import (
    DeviceBinding,
    DeviceBindingEvent,
    DeviceBindingEventAction,
    DeviceBindingStatus,
    HardDeleteViolation,
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@example.com", password=PASSWORD)


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(email="admin@example.com", password=PASSWORD)


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
        status=Esim.Status.ACTIVATED,
    )


def _auth(client, user):
    return {"HTTP_AUTHORIZATION": f"Bearer {_access_token(client, user.email)}"}


def _list_url(org_id):
    return f"/api/v1/orgs/{org_id}/device-bindings/"


def _detail_url(org_id, binding_id):
    return f"/api/v1/orgs/{org_id}/device-bindings/{binding_id}/"


def _unbind_url(org_id, binding_id):
    return f"/api/v1/orgs/{org_id}/device-bindings/{binding_id}/unbind/"


def _rotate_url(org_id, binding_id):
    return f"/api/v1/orgs/{org_id}/device-bindings/{binding_id}/credential/rotate/"


@pytest.mark.django_db
def test_create_binding_does_not_change_esim_account(client, owner, org, package):
    """MDM bind is enrollment only — never rewrites Esim.account (ADR 021)."""
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000009901",
    )
    account_before = esim.account_id
    resp = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    assert resp.status_code == 201, resp.content
    esim.refresh_from_db()
    assert esim.account_id == account_before


@pytest.mark.django_db
def test_rotate_credential_does_not_change_esim_account(client, owner, org, package):
    """Credential rotate must not rewrite Esim.account (ADR 021)."""
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000009902",
    )
    headers = _auth(client, owner)
    created = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **headers,
    )
    assert created.status_code == 201, created.content
    binding_id = created.json()["binding"]["id"]
    account_before = esim.account_id

    rotated = client.post(_rotate_url(org.pk, binding_id), **headers)
    assert rotated.status_code == 200, rotated.content
    assert rotated.json()["credential"]
    esim.refresh_from_db()
    assert esim.account_id == account_before


@pytest.mark.django_db
def test_owner_can_create_list_retrieve_unbind(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000001111",
    )
    headers = _auth(client, owner)

    create = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **headers,
    )
    assert create.status_code == 201, create.content
    payload = create.json()["binding"]
    assert create.json()["credential"]
    assert payload["status"] == DeviceBindingStatus.ACTIVE
    assert payload["esim_id"] == esim.pk
    assert payload["iccid"] == esim.iccid
    assert payload["device_external_id"]
    binding_id = payload["id"]

    assert DeviceBindingEvent.objects.filter(
        binding_id=binding_id, action=DeviceBindingEventAction.BIND
    ).exists()

    listed = client.get(_list_url(org.pk), **headers)
    assert listed.status_code == 200
    assert any(row["id"] == binding_id for row in listed.json())

    detail = client.get(_detail_url(org.pk, binding_id), **headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == binding_id

    unbound = client.post(_unbind_url(org.pk, binding_id), **headers)
    assert unbound.status_code == 200
    assert unbound.json()["status"] == DeviceBindingStatus.UNBOUND
    assert DeviceBindingEvent.objects.filter(
        binding_id=binding_id, action=DeviceBindingEventAction.UNBIND
    ).exists()


@pytest.mark.django_db
def test_admin_can_bind_member_viewer_cannot(
    client, owner, admin_user, member_user, viewer_user, org, package
):
    _add_member(org, admin_user, MembershipRole.ADMIN)
    _add_member(org, member_user, MembershipRole.MEMBER)
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000002222",
    )

    admin_create = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, admin_user),
    )
    assert admin_create.status_code == 201, admin_create.content

    for user in (member_user, viewer_user):
        esim2 = _make_esim(
            account=org.account,
            user=owner,
            package=package,
            iccid=f"89100000000000{user.pk:04d}",
        )
        resp = client.post(
            _list_url(org.pk),
            data=json.dumps({"esim_id": esim2.pk}),
            content_type="application/json",
            **_auth(client, user),
        )
        assert resp.status_code == 403


@pytest.mark.django_db
def test_viewer_can_list_but_not_unbind(client, owner, viewer_user, org, package):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000003333",
    )
    create = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    binding_id = create.json()["binding"]["id"]

    listed = client.get(_list_url(org.pk), **_auth(client, viewer_user))
    assert listed.status_code == 200
    assert any(row["id"] == binding_id for row in listed.json())

    unbound = client.post(_unbind_url(org.pk, binding_id), **_auth(client, viewer_user))
    assert unbound.status_code == 403


@pytest.mark.django_db
def test_personal_esim_not_bindable(client, owner, org, package):
    personal = ensure_billing_account(owner)
    esim = _make_esim(
        account=personal,
        user=owner,
        package=package,
        iccid="891000000000004444",
    )
    resp = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_cross_org_esim_not_found(client, owner, stranger, package):
    org_a = create_organization(name="A", actor=owner)
    org_b = create_organization(name="B", actor=stranger)
    esim_b = _make_esim(
        account=org_b.account,
        user=stranger,
        package=package,
        iccid="891000000000005555",
    )
    resp = client.post(
        _list_url(org_a.pk),
        data=json.dumps({"esim_id": esim_b.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_second_active_binding_conflicts_without_replace(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000006666",
    )
    headers = _auth(client, owner)
    first = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **headers,
    )
    assert first.status_code == 201
    second = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **headers,
    )
    assert second.status_code == 409


@pytest.mark.django_db
def test_replace_rebinds_and_audits(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000007777",
    )
    headers = _auth(client, owner)
    first = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **headers,
    )
    old_id = first.json()["binding"]["id"]
    old_device = first.json()["binding"]["device_external_id"]

    replaced = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk, "replace": True}),
        content_type="application/json",
        **headers,
    )
    assert replaced.status_code == 201, replaced.content
    new_id = replaced.json()["binding"]["id"]
    assert new_id != old_id
    assert replaced.json()["binding"]["device_external_id"] != old_device
    assert replaced.json()["credential"]

    old = DeviceBinding.objects.get(pk=old_id)
    assert old.status == DeviceBindingStatus.REPLACED
    assert str(old.replaced_by_id) == new_id
    assert DeviceBindingEvent.objects.filter(
        binding_id=new_id, action=DeviceBindingEventAction.REBIND
    ).exists()
    assert (
        DeviceBinding.objects.filter(
            esim=esim, status=DeviceBindingStatus.ACTIVE
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_rejects_client_account_id_and_device_external_id(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000008888",
    )
    headers = _auth(client, owner)
    resp = client.post(
        _list_url(org.pk),
        data=json.dumps(
            {
                "esim_id": esim.pk,
                "account_id": str(org.account_id),
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert resp.status_code == 400
    assert "account_id" in resp.json()

    resp2 = client.post(
        _list_url(org.pk),
        data=json.dumps(
            {
                "esim_id": esim.pk,
                "device_external_id": "client-forged-id",
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert resp2.status_code == 400
    assert "device_external_id" in resp2.json()


@pytest.mark.django_db
def test_suspended_org_blocks_bind(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000009999",
    )
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    resp = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_hard_delete_blocked(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="891000000000000001",
    )
    create = client.post(
        _list_url(org.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, owner),
    )
    binding = DeviceBinding.objects.get(pk=create.json()["binding"]["id"])
    with pytest.raises(HardDeleteViolation):
        binding.delete()
    event = DeviceBindingEvent.objects.filter(binding=binding).first()
    assert event is not None
    with pytest.raises(HardDeleteViolation):
        event.delete()


@pytest.mark.django_db
def test_foreign_org_binding_detail_404(client, owner, stranger, package):
    org_a = create_organization(name="A", actor=owner)
    org_b = create_organization(name="B", actor=stranger)
    esim = _make_esim(
        account=org_b.account,
        user=stranger,
        package=package,
        iccid="891000000000000002",
    )
    create = client.post(
        _list_url(org_b.pk),
        data=json.dumps({"esim_id": esim.pk}),
        content_type="application/json",
        **_auth(client, stranger),
    )
    binding_id = create.json()["binding"]["id"]
    resp = client.get(_detail_url(org_a.pk, binding_id), **_auth(client, owner))
    assert resp.status_code == 404
