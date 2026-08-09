"""PR18 device status + UEM ICCID lookup (ADR 021 staging proof override)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.exceptions import IccidNotFoundError
from apps.organizations.models import DeviceBindingStatus
from apps.organizations.services import create_device_binding, create_organization
from apps.organizations.services.device_status import get_device_status_by_credential

User = get_user_model()
PASSWORD = "SecurePass1!"
UEM_GUID = "bc473029-90d8-4476-bb79-3ac6eb17725d"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner-uem@example.com", password=PASSWORD)


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-uem-1gb",
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
    return create_organization(name="UEM Proof Org", actor=owner)


def _make_esim(*, account, user, package: Package, iccid: str) -> Esim:
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-6:]}",
        customer_ref=f"ref-{iccid[-6:]}",
    )
    return Esim.objects.create(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        status=Esim.Status.INSTALLED,
        usage_is_unlimited=True,
        usage_expired_at=timezone.now() + timedelta(days=7),
    )


def _device_status(client, *, device_external_id: str, credential: str):
    return client.post(
        "/api/v1/device/status/",
        data=json.dumps(
            {
                "device_external_id": device_external_id,
                "credential": credential,
            }
        ),
        content_type="application/json",
    )


def _bind_with_uem_guid(*, owner, org, package, bound_iccid: str, uem_guid: str):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid=bound_iccid,
    )
    result = create_device_binding(owner, org.id, esim_id=esim.pk)
    binding = result.binding
    binding.uem_device_guid = uem_guid
    binding.save(update_fields=["uem_device_guid", "updated_at"])
    return result.binding, result.credential, esim


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_uem_path_returns_status_for_matching_team_iccid(client, owner, org, package):
    binding, credential, bound_esim = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000001",
        uem_guid=UEM_GUID,
    )
    active_esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="89852350326100304891",
    )
    assert active_esim.pk != bound_esim.pk

    device = {
        "guid": UEM_GUID,
        "iccid": "89852350326100304891",
        "sims": [{"iccid": "89852350326100304891", "homeCarrier": "Connect"}],
    }
    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_guid.return_value = device
        resp = _device_status(
            client,
            device_external_id=binding.device_external_id,
            credential=credential,
        )

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["esim"]["iccid"] == "89852350326100304891"
    assert body["esim"]["id"] == active_esim.pk
    assert body["usage"]["data_remaining"] == "unlimited"
    # No mutation of binding.esim
    binding.refresh_from_db()
    assert binding.esim_id == bound_esim.pk


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_uem_path_miss_when_iccid_not_on_team_account(client, owner, org, package):
    binding, credential, _ = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000002",
        uem_guid=UEM_GUID,
    )
    device = {
        "guid": UEM_GUID,
        "iccid": "8999999999999999999",
        "sims": [{"iccid": "8999999999999999999"}],
    }
    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_guid.return_value = device
        resp = _device_status(
            client,
            device_external_id=binding.device_external_id,
            credential=credential,
        )

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "iccid_not_found"
    assert "ICCID" in body["detail"]


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_uem_path_unavailable_when_sims_empty(client, owner, org, package):
    binding, credential, _ = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000003",
        uem_guid=UEM_GUID,
    )
    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_guid.return_value = {
            "guid": UEM_GUID,
            "iccid": None,
            "sims": [],
        }
        resp = _device_status(
            client,
            device_external_id=binding.device_external_id,
            credential=credential,
        )

    assert resp.status_code == 503
    assert resp.json()["code"] == "uem_inventory_unavailable"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=False)
def test_uem_path_unavailable_when_integration_disabled(client, owner, org, package):
    binding, credential, _ = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000004",
        uem_guid=UEM_GUID,
    )
    resp = _device_status(
        client,
        device_external_id=binding.device_external_id,
        credential=credential,
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "uem_inventory_unavailable"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_classic_pr18_when_uem_guid_empty(client, owner, org, package):
    esim = _make_esim(
        account=org.account,
        user=owner,
        package=package,
        iccid="8900000000000000005",
    )
    result = create_device_binding(owner, org.id, esim_id=esim.pk)
    assert result.binding.uem_device_guid == ""

    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient"
    ) as client_cls:
        resp = _device_status(
            client,
            device_external_id=result.binding.device_external_id,
            credential=result.credential,
        )
        client_cls.assert_not_called()

    assert resp.status_code == 200
    assert resp.json()["esim"]["iccid"] == "8900000000000000005"


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_uem_path_does_not_create_esim_on_miss(owner, org, package):
    binding, credential, _ = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000006",
        uem_guid=UEM_GUID,
    )
    before = Esim.objects.filter(account=org.account).count()
    device = {
        "guid": UEM_GUID,
        "iccid": "8977777777777777777",
        "sims": [{"iccid": "8977777777777777777"}],
    }
    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient"
    ) as client_cls:
        client_cls.return_value.get_device_by_guid.return_value = device
        with pytest.raises(IccidNotFoundError):
            get_device_status_by_credential(
                device_external_id=binding.device_external_id,
                credential=credential,
            )

    assert Esim.objects.filter(account=org.account).count() == before
    binding.refresh_from_db()
    assert binding.status == DeviceBindingStatus.ACTIVE


@pytest.mark.django_db
@override_settings(BLACKBERRY_UEM_ENABLED=True)
def test_uem_client_error_maps_to_unavailable(client, owner, org, package):
    from apps.integrations.blackberry_uem.client import BlackberryUemClientError

    binding, credential, _ = _bind_with_uem_guid(
        owner=owner,
        org=org,
        package=package,
        bound_iccid="8900000000000000007",
        uem_guid=UEM_GUID,
    )
    mock_client = MagicMock()
    mock_client.get_device_by_guid.side_effect = BlackberryUemClientError("boom")
    with patch(
        "apps.organizations.services.device_status.BlackberryUemClient",
        return_value=mock_client,
    ):
        resp = _device_status(
            client,
            device_external_id=binding.device_external_id,
            credential=credential,
        )
    assert resp.status_code == 503
    assert resp.json()["code"] == "uem_inventory_unavailable"
