"""GET /api/v1/partner/channels/{id}/customers/{id}/plans/ (ADR 024)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import Account
from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)
_PLAN_KEYS = {
    "location_title",
    "package_title",
    "data_allowance",
    "validity_days",
    "status",
    "usage_remaining_mb",
    "usage_total_mb",
    "usage_is_unlimited",
    "usage_expired_at",
    "usage_synced_at",
    "created_at",
}
_FORBIDDEN = {
    "iccid",
    "matching_id",
    "lpa",
    "qr",
    "qrcode",
    "activation_code",
    "confirmation_code",
    "installation_url",
    "archived_at",
    "account_id",
    "order_id",
    "ledger_entry_id",
    "esim_id",
    "customer_id",
    "count",
    "id",
}


@pytest.fixture
def client() -> Client:
    return Client()


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _url(channel_id: uuid.UUID, customer_id: int) -> str:
    return f"/api/v1/partner/channels/{channel_id}/customers/{customer_id}/plans/"


def _get(client: Client, user: User | None, channel_id: uuid.UUID, customer_id: int):
    headers = _auth(user) if user is not None else {}
    return client.get(_url(channel_id, customer_id), **headers)


def _team(actor: User, name: str) -> PartnerChannel:
    org = create_organization(name=name, actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=True,
    )


def _membership(
    *,
    user: User,
    channel: PartnerChannel,
    role: str,
    status: str = MembershipStatus.ACTIVE,
) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=status,
    )


def _attribute(customer: User, channel: PartnerChannel) -> None:
    CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _esim(
    customer: User,
    *,
    status: str,
    package_title: str,
    location_title: str = "Croatia",
    data_allowance: str = "5 GB",
    validity_days: int | None = 30,
    usage_remaining_mb: int | None = 4123,
    usage_total_mb: int | None = 5120,
    usage_is_unlimited: bool | None = False,
    usage_expired_at: datetime | None = None,
    usage_synced_at: datetime | None = None,
    created_at: datetime | None = None,
    archived_at: datetime | None = None,
    iccid: str | None = None,
) -> Esim:
    package = Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="Catalog title",
        operator_title="Op",
        country_code="HR",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=customer.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        location_title=location_title,
        package_title=package_title,
        data_allowance=data_allowance,
        validity_days=validity_days,
    )
    esim = Esim.objects.create(
        user=customer,
        account=customer.billing_account,
        order=order,
        iccid=iccid or f"89{uuid.uuid4().int % 10**18:018d}"[:20],
        lpa=f"LPA:SECRET-{uuid.uuid4()}",
        matching_id=f"MATCH-SECRET-{uuid.uuid4()}",
        qrcode=f"QR-SECRET-{uuid.uuid4()}",
        status=status,
        usage_remaining_mb=usage_remaining_mb,
        usage_total_mb=usage_total_mb,
        usage_is_unlimited=usage_is_unlimited,
        usage_expired_at=usage_expired_at,
        usage_synced_at=usage_synced_at,
        archived_at=archived_at,
    )
    if created_at is not None:
        Esim.objects.filter(pk=esim.pk).update(created_at=created_at)
        esim.refresh_from_db()
    return esim


def _keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(value)
        for item in value.values():
            found.update(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_keys(item))
    return found


@ENABLED
@pytest.mark.django_db
def test_owner_receives_the_locked_plan_shape(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _attribute(customer, channel)
    created = datetime(2026, 10, 1, 8, 10, tzinfo=UTC)
    synced = datetime(2026, 10, 10, 17, 42, tzinfo=UTC)
    expires = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    _esim(
        customer,
        status=Esim.Status.IN_USE,
        package_title="Croatia 5 GB - 30 Days",
        created_at=created,
        usage_synced_at=synced,
        usage_expired_at=expires,
        iccid="8901SECRETICCID0001",
    )
    accounts = Account.objects.count()

    with patch(
        "apps.esims.services.usage_service.UsageService.get_usage",
    ) as get_usage:
        response = _get(client, owner, channel.pk, customer.pk)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Partner-Role"] == "owner"
    body = response.json()
    assert set(body) == {"active", "expired"}
    assert body["expired"] == []
    assert len(body["active"]) == 1
    plan = body["active"][0]
    assert set(plan) == _PLAN_KEYS
    assert plan["location_title"] == "Croatia"
    assert plan["package_title"] == "Croatia 5 GB - 30 Days"
    assert plan["data_allowance"] == "5 GB"
    assert plan["validity_days"] == 30
    assert plan["status"] == "in_use"
    assert plan["usage_remaining_mb"] == 4123
    assert plan["usage_total_mb"] == 5120
    assert plan["usage_is_unlimited"] is False
    assert plan["usage_expired_at"].startswith("2026-11-08T12:00:00")
    assert plan["usage_synced_at"].startswith("2026-10-10T17:42:00")
    assert plan["created_at"].startswith("2026-10-01T08:10:00")
    raw = response.content.decode()
    assert "8901SECRETICCID0001" not in raw
    assert "LPA:SECRET" not in raw
    assert "MATCH-SECRET" not in raw
    assert "QR-SECRET" not in raw
    assert str(customer.billing_account.pk) not in raw
    assert _keys(body).isdisjoint(_FORBIDDEN)
    assert Account.objects.count() == accounts
    get_usage.assert_not_called()


@ENABLED
@pytest.mark.django_db
def test_admin_sees_plans_and_empty_strings_stay_empty(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _attribute(customer, channel)
    _esim(
        customer,
        status=Esim.Status.PURCHASED,
        location_title="",
        package_title="",
        data_allowance="",
        validity_days=None,
        usage_remaining_mb=None,
        usage_total_mb=None,
        usage_is_unlimited=None,
        usage_expired_at=None,
        usage_synced_at=None,
    )

    response = _get(client, admin, channel.pk, customer.pk)

    assert response.status_code == 200
    assert response["X-Partner-Role"] == "admin"
    plan = response.json()["active"][0]
    assert plan["location_title"] == ""
    assert plan["package_title"] == ""
    assert plan["data_allowance"] == ""
    assert plan["validity_days"] is None
    assert plan["usage_remaining_mb"] is None
    assert plan["usage_total_mb"] is None
    assert plan["usage_is_unlimited"] is None
    assert plan["usage_expired_at"] is None
    assert plan["usage_synced_at"] is None
    assert plan["status"] == "purchased"
    assert plan["created_at"]


@ENABLED
@pytest.mark.django_db
def test_grouping_omits_archived_and_sorts_null_expiry_last(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _attribute(customer, channel)
    same = datetime(2026, 8, 1, tzinfo=UTC)
    _esim(
        customer,
        status=Esim.Status.ACTIVATED,
        package_title="Older active",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _esim(
        customer,
        status=Esim.Status.INSTALLED,
        package_title="Newer active",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    first = _esim(
        customer,
        status=Esim.Status.IN_USE,
        package_title="Tie low",
        created_at=same,
    )
    second = _esim(
        customer,
        status=Esim.Status.IN_USE,
        package_title="Tie high",
        created_at=same,
    )
    assert second.pk > first.pk
    _esim(
        customer,
        status=Esim.Status.EXPIRED,
        package_title="Later expiry",
        usage_expired_at=datetime(2026, 12, 1, tzinfo=UTC),
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    _esim(
        customer,
        status=Esim.Status.EXPIRED,
        package_title="Known expiry",
        usage_expired_at=datetime(2026, 11, 1, tzinfo=UTC),
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    _esim(
        customer,
        status=Esim.Status.EXPIRED,
        package_title="Null expiry",
        usage_expired_at=None,
        created_at=datetime(2026, 12, 2, tzinfo=UTC),
    )
    _esim(
        customer,
        status=Esim.Status.EXPIRED,
        package_title="ARCHIVED-PLAN",
        archived_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    with CaptureQueriesContext(connection) as captured:
        response = _get(client, owner, channel.pk, customer.pk)

    assert response.status_code == 200
    body = response.json()
    assert [row["package_title"] for row in body["active"]] == [
        "Tie high",
        "Tie low",
        "Newer active",
        "Older active",
    ]
    assert [row["package_title"] for row in body["expired"]] == [
        "Later expiry",
        "Known expiry",
        "Null expiry",
    ]
    raw = response.content.decode()
    assert "ARCHIVED-PLAN" not in raw
    assert "archived" not in raw
    esim_queries = [
        item["sql"] for item in captured.captured_queries if "esims_esim" in item["sql"]
    ]
    assert len(esim_queries) == 1
    assert "orders_order" in esim_queries[0]


@ENABLED
@pytest.mark.django_db
def test_missing_account_is_empty_and_is_not_created(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _attribute(customer, channel)
    customer.billing_account.delete()
    accounts = Account.objects.count()

    response = _get(client, owner, channel.pk, customer.pk)

    assert response.status_code == 200
    assert response.json() == {"active": [], "expired": []}
    assert Account.objects.count() == accounts


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "role",
    [MembershipRole.MEMBER, MembershipRole.VIEWER],
)
def test_member_and_viewer_cannot_read_plans(client: Client, role: str) -> None:
    owner = _user("owner")
    actor = _user("actor")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _membership(user=actor, channel=channel, role=role)
    _attribute(customer, channel)

    response = _get(client, actor, channel.pk, customer.pk)

    assert response.status_code == 403
    assert response.json() == {"code": "partner_access_denied"}


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [MembershipStatus.SUSPENDED, MembershipStatus.REVOKED],
)
def test_suspended_and_revoked_cannot_read_plans(client: Client, status: str) -> None:
    owner = _user("owner")
    actor = _user("actor")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    _membership(
        user=actor,
        channel=channel,
        role=MembershipRole.ADMIN,
        status=status,
    )
    _attribute(customer, channel)

    response = _get(client, actor, channel.pk, customer.pk)

    assert response.status_code == 403
    assert response.json() == {"code": "partner_access_denied"}


@ENABLED
@pytest.mark.django_db
def test_foreign_channel_is_denied(client: Client) -> None:
    owner = _user("owner")
    other = _user("other")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    foreign = _team(other, "Other")
    _attribute(customer, channel)

    response = _get(client, owner, foreign.pk, customer.pk)

    assert response.status_code == 403
    assert response.json() == {"code": "partner_access_denied"}


@ENABLED
@pytest.mark.django_db
def test_foreign_customer_is_not_found(client: Client) -> None:
    owner = _user("owner")
    other = _user("other")
    customer = _user("customer")
    channel = _team(owner, "Fleet")
    elsewhere = _team(other, "Other")
    _attribute(customer, elsewhere)

    missing = _get(client, owner, channel.pk, customer.pk)
    unknown = _get(client, owner, channel.pk, 999_999_999)

    assert missing.status_code == 404
    assert missing.json() == {"code": "customer_not_found"}
    assert unknown.status_code == 404
    assert unknown.json() == {"code": "customer_not_found"}


@override_settings(PARTNER_CHANNEL_ENABLED=False, BILLING_ENABLED=True)
@pytest.mark.django_db
def test_disabled_flag_hides_plans(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _team(owner, "Fleet")

    response = _get(client, owner, channel.pk, customer.pk)

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
