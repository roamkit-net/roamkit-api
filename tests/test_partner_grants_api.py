"""GET /api/v1/orgs/partner/grants/ (ADR 023)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
)
from apps.billing.services.credit import credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/orgs/partner/grants/"
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


def _grant(
    channel: PartnerChannel,
    *,
    customer: User,
    amount: str,
    granted_by: User | None,
) -> PartnerCreditGrant:
    attribution = CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )

    def ledger():
        return credit_service.credit(
            channel.organization.account,
            Decimal("0.100000"),
            reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
            reference_id=f"grant-{uuid.uuid4()}",
            idempotency_key=f"grant-{uuid.uuid4()}",
        )

    return PartnerCreditGrant.objects.create(
        partner_channel=channel,
        customer_user=customer,
        customer_user_id_snapshot=customer.pk,
        customer_attribution=attribution,
        granted_by=granted_by,
        granted_by_user_id_snapshot=1 if granted_by is None else granted_by.pk,
        amount=Decimal(amount),
        idempotency_key=f"idem-{uuid.uuid4()}",
        debit_ledger_entry=ledger(),
        credit_ledger_entry=ledger(),
    )


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

    response = _get(client, owner, sort="email")

    assert response.status_code == 404
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {"code": "partner_channel_disabled"}


@ENABLED
@pytest.mark.django_db
def test_grants_return_snapshot_customer_and_masked_email(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    customer = _user("ada")
    grant = _grant(channel, customer=customer, amount="1.250000", granted_by=owner)

    response = _get(client, owner, q=customer.email)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["count"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    row = body["results"][0]
    assert set(row) == {
        "grant_id",
        "customer_id",
        "email",
        "amount",
        "granted_by",
        "created_at",
    }
    assert row["grant_id"] == str(grant.id)
    assert row["customer_id"] == customer.pk
    assert row["email"] == f"a***@{customer.email.split('@', 1)[1]}"
    assert customer.email not in response.content.decode()
    assert owner.email not in response.content.decode()
    assert row["amount"] == "1.250000"
    assert row["granted_by"] == {
        "user_id": owner.pk,
        "email": f"o***@{owner.email.split('@', 1)[1]}",
    }
    assert grant.idempotency_key not in response.content.decode()


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
    assert allowed.json()["count"] == 0
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_access_denied"}


@ENABLED
@pytest.mark.django_db
def test_bad_sort_uses_only_the_code_envelope(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)

    response = _get(client, owner, sort="email")

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_sort"}


@ENABLED
@pytest.mark.django_db
def test_page_past_the_end_is_empty(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    _grant(channel, customer=_user("ada"), amount="1.000000", granted_by=owner)

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
def test_inactive_channel_still_lists_grants(client: Client) -> None:
    owner = _user("owner")
    channel = _channel_for(owner, active=False)
    customer = _user("ada")
    grant = _grant(channel, customer=customer, amount="4.000000", granted_by=None)

    response = _get(client, owner)

    assert response.status_code == 200
    row = response.json()["results"][0]
    assert row["grant_id"] == str(grant.id)
    assert row["granted_by"] is None
