"""GET /api/v1/partner/channels/{channel_id}/summary/ (ADR 024)."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_settlement import PartnerChannelOwnershipInvalid
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
LEGACY_URL = "/api/v1/orgs/partner/summary/"
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


def _summary_url(channel_id: uuid.UUID | str) -> str:
    return f"/api/v1/partner/channels/{channel_id}/summary/"


def _get(client: Client, user: User | None, channel_id: uuid.UUID | str):
    headers = _auth(user) if user is not None else {}
    return client.get(_summary_url(channel_id), **headers)


def _legacy(client: Client, user: User):
    return client.get(LEGACY_URL, **_auth(user))


def _individual(owner: User) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("25.00"),
    )


def _team(actor: User, name: str) -> PartnerChannel:
    org = create_organization(name=name, actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
    )


def _credit(account, amount: str) -> None:
    credit_service.credit(
        account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"sum-{uuid.uuid4()}",
        idempotency_key=f"sum-{uuid.uuid4()}",
    )


def _accrual(channel: PartnerChannel, account, partner_share: str) -> None:
    entry = credit_service.credit(
        account,
        Decimal("0.250000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"accrual-{uuid.uuid4()}",
        idempotency_key=f"accrual-{uuid.uuid4()}",
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


def _snapshot() -> tuple:
    return (
        list(Account.objects.order_by("id").values_list("id", "balance", "kind")),
        CreditLedgerEntry.objects.count(),
        CustomerAttribution.objects.count(),
        list(Membership.objects.order_by("id").values_list("id", "role", "status")),
        list(
            PartnerChannel.objects.order_by("id").values_list(
                "id",
                "kind",
                "owner_user_id",
                "organization_id",
            )
        ),
    )


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required(client: Client) -> None:
    response = _get(client, None, uuid.uuid4())

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_flag_off_is_disabled(client: Client, settings) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    channel = _team(owner, "Fleet")

    response = _get(client, owner, channel.pk)

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_team_summary_matches_legacy_and_does_not_write(client: Client) -> None:
    owner = _user("owner")
    channel = _team(owner, "Fleet")
    _credit(owner.billing_account, "9.000000")
    _accrual(channel, channel.organization.account, "1.500000")
    before = _snapshot()

    legacy = _legacy(client, owner)
    response = _get(client, owner, channel.pk)

    assert legacy.status_code == 200
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Partner-Role"] == "owner"
    assert response.json() == legacy.json()
    assert response.json()["total_earned"] == "1.500000"
    assert response.json()["available_balance"] != "9.000000"
    assert _snapshot() == before


@ENABLED
@pytest.mark.django_db
def test_individual_summary_uses_personal_balance(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    _credit(owner.billing_account, "8.000000")
    _accrual(channel, owner.billing_account, "1.250000")
    before = _snapshot()

    response = _get(client, owner, channel.pk)
    legacy = _legacy(client, owner)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Partner-Role"] == "owner"
    assert response.json()["total_earned"] == "1.250000"
    assert response.json()["available_balance"] == "8.250000"
    assert legacy.status_code == 403
    assert legacy.json() == {"code": "partner_access_denied"}
    assert _snapshot() == before


@ENABLED
@pytest.mark.django_db
def test_viewer_can_read_and_inaccessible_channels_match(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    stranger = _user("stranger")
    suspended = _user("suspended")
    channel = _team(owner, "Fleet")
    _accrual(channel, channel.organization.account, "2.000000")
    Membership.objects.create(
        organization=channel.organization,
        user=viewer,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.ACTIVE,
    )
    Membership.objects.create(
        organization=channel.organization,
        user=suspended,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.SUSPENDED,
    )
    foreign = _individual(stranger)
    unknown = uuid.uuid4()

    allowed = _get(client, viewer, channel.pk)
    denied_foreign = _get(client, viewer, foreign.pk)
    denied_unknown = _get(client, viewer, unknown)
    denied_suspended = _get(client, suspended, channel.pk)

    assert allowed.status_code == 200
    assert allowed["X-Partner-Role"] == "viewer"
    assert allowed.json()["total_earned"] == "2.000000"
    for denied in (denied_foreign, denied_unknown, denied_suspended):
        assert denied.status_code == 403
        assert denied.json() == {"code": "partner_access_denied"}
        assert "X-Partner-Role" not in denied
        assert "available_balance" not in denied.json()
    assert denied_foreign.content == denied_unknown.content


@ENABLED
@pytest.mark.django_db
def test_requested_channel_does_not_fall_back(client: Client) -> None:
    user = _user("user")
    individual = _individual(user)
    team = _team(user, "Fleet")
    _credit(user.billing_account, "5.000000")
    _credit(team.organization.account, "7.000000")
    other = _individual(_user("other"))

    own = _get(client, user, individual.pk)
    team_response = _get(client, user, team.pk)
    denied = _get(client, user, other.pk)

    assert own.json()["available_balance"] == "5.000000"
    assert team_response.json()["available_balance"] == "7.000000"
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_access_denied"}
    assert _legacy(client, user).status_code == 200


@ENABLED
@pytest.mark.django_db
def test_two_team_channels_stay_ambiguous_on_the_legacy_route(client: Client) -> None:
    owner = _user("owner")
    first = _team(owner, "Alpha")
    second = _team(_user("other"), "Beta")
    Membership.objects.create(
        organization=second.organization,
        user=owner,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.ACTIVE,
    )
    _credit(first.organization.account, "1.000000")
    _credit(second.organization.account, "2.000000")

    legacy = _legacy(client, owner)
    first_response = _get(client, owner, first.pk)
    second_response = _get(client, owner, second.pk)

    assert legacy.status_code == 409
    assert legacy.json() == {"code": "partner_context_ambiguous"}
    assert first_response.json()["available_balance"] == "1.000000"
    assert second_response.json()["available_balance"] == "2.000000"


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_returns_stored_history(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    channel.is_active = False
    channel.save(update_fields=["is_active"])
    _accrual(channel, owner.billing_account, "3.000000")

    response = _get(client, owner, channel.pk)

    assert response.status_code == 200
    assert response.json()["total_earned"] == "3.000000"
    assert response.json()["accrual_counts"]["total"] == 1


@ENABLED
@pytest.mark.django_db
def test_malformed_channel_path_is_a_normal_404(client: Client) -> None:
    owner = _user("owner")

    response = client.get(
        "/api/v1/partner/channels/not-a-uuid/summary/",
        **_auth(owner),
    )

    assert response.status_code == 404
    assert b"partner_access_denied" not in response.content
    assert b"partner_settlement_account_missing" not in response.content


@ENABLED
@pytest.mark.django_db
def test_missing_settlement_account_is_generic_500(
    client: Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = _user("owner")
    personal = Account.objects.get(user=owner)
    personal_id = personal.pk
    Account.objects.filter(pk=personal_id).delete()
    channel = _individual(owner)

    with caplog.at_level(logging.ERROR):
        response = _get(client, owner, channel.pk)

    assert response.status_code == 500
    assert response.json() == {"code": "partner_settlement_account_missing"}
    assert response["Cache-Control"] == "no-store"
    assert str(personal_id) not in response.content.decode()
    assert "error_type=PartnerSettlementAccountMissing" in caplog.text
    assert "error_type=PartnerChannelOwnershipInvalid" not in caplog.text
    assert Account.objects.filter(pk=personal_id).count() == 0


@ENABLED
@pytest.mark.django_db
def test_invalid_ownership_logs_its_own_type(
    client: Client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = _user("owner")
    channel = _team(owner, "Fleet")
    message = "kind and owner columns disagree"

    with (
        caplog.at_level(logging.ERROR),
        patch(
            "apps.billing.partner_portal_views.resolve_partner_settlement_account",
            side_effect=PartnerChannelOwnershipInvalid(message),
        ),
    ):
        response = _get(client, owner, channel.pk)

    assert response.status_code == 500
    assert response.json() == {"code": "partner_settlement_account_missing"}
    assert message not in response.content.decode()
    assert str(channel.organization.account_id) not in response.content.decode()
    assert "error_type=PartnerChannelOwnershipInvalid" in caplog.text
    assert "error_type=PartnerSettlementAccountMissing" not in caplog.text
