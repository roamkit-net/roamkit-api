"""GET /api/v1/orgs/partner/summary/ (ADR 023)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import PartnerChannel, PartnerMarginAccrual
from apps.billing.services.credit import credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/orgs/partner/summary/"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


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


def _accrual(channel: PartnerChannel, partner_share: str) -> None:
    entry = credit_service.credit(
        channel.organization.account,
        Decimal("1.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"sum-{uuid.uuid4()}",
        idempotency_key=f"sum-{uuid.uuid4()}",
    )
    PartnerMarginAccrual.objects.create(
        partner_channel=channel,
        customer_user_id_snapshot=1,
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=f"order-{uuid.uuid4()}",
        list_price=Decimal("10.000000"),
        net_price=Decimal("4.000000"),
        margin=Decimal("6.000000"),
        revenue_share_percent=Decimal("10.00"),
        partner_share=Decimal(partner_share),
        ledger_entry=entry,
    )


def _get(client: Client, user: User | None):
    headers = _auth(user) if user is not None else {}
    return client.get(URL, **headers)


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required(client: Client) -> None:
    response = _get(client, None)

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_flag_off_is_disabled(client: Client, settings) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    _channel_for(owner)

    response = _get(client, owner)

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_summary_returns_stored_totals(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    _accrual(channel, "6.666000")
    _accrual(channel, "1.000000")

    response = _get(client, owner)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Partner-Role"] == "owner"
    assert response.json() == {
        "total_earned": "7.666000",
        "available_balance": "2.000000",
        "accrual_counts": {
            "order": 2,
            "topup": 0,
            "subscription": 0,
            "total": 2,
        },
    }


@ENABLED
@pytest.mark.django_db
def test_viewer_may_read_and_member_may_not(client: Client) -> None:
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

    allowed = _get(client, viewer)
    denied = _get(client, member)

    assert allowed.status_code == 200
    assert allowed.json()["total_earned"] == "0.000000"
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_access_denied"}


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
def test_inactive_channel_still_returns_history(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner, active=False)
    _accrual(channel, "3.000000")

    response = _get(client, owner)

    assert response.status_code == 200
    assert response.json()["total_earned"] == "3.000000"
    assert response.json()["accrual_counts"]["total"] == 1
