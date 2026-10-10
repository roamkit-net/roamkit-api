"""GET /api/v1/orgs/partner/customers/ (ADR 023)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.credit import credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/orgs/partner/customers/"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)
_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


def _channel_for(actor: User, *, active: bool = True) -> PartnerChannel:
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=active,
    )


def _customer(channel: PartnerChannel, prefix: str) -> User:
    customer = _user(prefix)
    CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=_AT,
    )
    entry = credit_service.credit(
        channel.organization.account,
        Decimal("0.250000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"cust-{uuid.uuid4()}",
        idempotency_key=f"cust-{uuid.uuid4()}",
    )
    PartnerMarginAccrual.objects.create(
        partner_channel=channel,
        customer_user_id_snapshot=customer.pk,
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=f"order-{uuid.uuid4()}",
        list_price=Decimal("10.000000"),
        net_price=Decimal("4.000000"),
        margin=Decimal("6.000000"),
        revenue_share_percent=Decimal("10.00"),
        partner_share=Decimal("1.250000"),
        ledger_entry=entry,
    )
    return customer


def _get(client: Client, user: User | None, **params: str):
    headers = _auth(user) if user is not None else {}
    return client.get(URL, params, **headers)


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required(client: Client) -> None:
    response = _get(client, None)

    assert response.status_code == 401
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {"code": "authentication_required"}


@pytest.mark.django_db
def test_disabled_flag_hides_the_list(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)

    response = _get(client, owner, sort="nope")

    assert response.status_code == 404
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {"code": "partner_channel_disabled"}


@ENABLED
@pytest.mark.django_db
def test_customers_return_full_email_and_snapshot_earnings(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    customer = _customer(channel, "ada")

    response = _get(client, owner)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["count"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    row = body["results"][0]
    assert row["customer_id"] == customer.pk
    assert row["email"] == customer.email
    assert row["display_name"] == ""
    assert customer.email in response.content.decode()
    assert row["total_partner_earned"] == "1.250000"
    assert row["accrual_count"] == 1
    assert row["attributed_at"].startswith("2026-10-01T12:00:00")


@ENABLED
@pytest.mark.django_db
def test_viewer_and_member_may_read_customers(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    member = _user("member")
    channel = _channel_for(owner)
    Membership.objects.create(
        organization=channel.organization,
        user=viewer,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.ACTIVE,
    )
    Membership.objects.create(
        organization=channel.organization,
        user=member,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )

    viewer_response = _get(client, viewer)
    member_response = _get(client, member)

    assert viewer_response.status_code == 200
    assert viewer_response.json()["count"] == 0
    assert member_response.status_code == 200
    assert member_response["X-Partner-Role"] == "member"
    assert member_response.json()["count"] == 0


@ENABLED
@pytest.mark.django_db
def test_bad_query_uses_only_the_code_envelope(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)

    sort = _get(client, owner, sort="email")
    query = _get(client, owner, q="x" * 255)

    assert sort.status_code == 400
    assert sort.json() == {"code": "invalid_sort"}
    assert query.status_code == 400
    assert query.json() == {"code": "invalid_query"}


@ENABLED
@pytest.mark.django_db
def test_page_past_the_end_is_empty(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    _customer(channel, "ada")

    response = _get(client, owner, page="3", page_size="1")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["page"] == 3
    assert response.json()["results"] == []


@ENABLED
@pytest.mark.django_db
def test_two_channels_are_ambiguous(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)
    _channel_for(owner)

    response = _get(client, owner)

    assert response.status_code == 409
    assert response.json() == {"code": "partner_context_ambiguous"}


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_lists_current_customers(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner, active=False)
    customer = _customer(channel, "ada")

    response = _get(client, owner)

    assert response.status_code == 200
    assert response.json()["results"][0]["customer_id"] == customer.pk
