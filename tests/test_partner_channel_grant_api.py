"""POST /api/v1/partner/channels/{channel_id}/grants/ (ADR 024)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import (
    Account,
    AccountKind,
    CreditLedgerEntry,
    LedgerReferenceType,
)
from apps.billing.partner_channel import (
    CustomerAttribution,
    CustomerAttributionHistory,
    PartnerChannel,
    PartnerCreditGrant,
    PendingPartnerAttribution,
)
from apps.billing.services.credit import credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
LEGACY_URL = "/api/v1/billing/partner-grants/"
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


def _url(channel_id: uuid.UUID | str) -> str:
    return f"/api/v1/partner/channels/{channel_id}/grants/"


def _post(
    client: Client,
    user: User | None,
    channel_id: uuid.UUID | str,
    body: object,
    *,
    raw: str | None = None,
):
    headers = _auth(user) if user is not None else {}
    payload = raw if raw is not None else json.dumps(body)
    return client.post(
        _url(channel_id),
        data=payload,
        content_type="application/json",
        HTTP_X_REQUEST_ID="req-channel-grant",
        **headers,
    )


def _legacy(client: Client, user: User, body: dict):
    return client.post(
        LEGACY_URL,
        data=json.dumps(body),
        content_type="application/json",
        **_auth(user),
    )


def _body(customer: User, amount: str = "10.000000", key: str = "grant-1") -> dict:
    return {
        "customer_id": customer.pk,
        "amount": amount,
        "idempotency_key": key,
    }


def _individual(owner: User, *, active: bool = True) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("25.00"),
        is_active=active,
    )


def _team(actor: User, name: str, *, active: bool = True) -> PartnerChannel:
    org = create_organization(name=name, actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=active,
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


def _attribute(customer: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


def _credit(account: Account, amount: str, reference: str) -> None:
    credit_service.credit(
        account,
        Decimal(amount),
        reference_type=reference,
        reference_id=f"fund-{uuid.uuid4()}",
        idempotency_key=f"fund-{uuid.uuid4()}",
    )
    account.refresh_from_db()


def _grant_rows() -> int:
    return CreditLedgerEntry.objects.filter(
        reference_type__in=[
            LedgerReferenceType.PARTNER_GRANT_OUT,
            LedgerReferenceType.PARTNER_GRANT_IN,
        ]
    ).count()


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required_before_the_body(client: Client) -> None:
    response = _post(client, None, uuid.uuid4(), {"amount": "nope"})

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_flag_off_hides_the_route_before_the_body(client: Client, settings) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    channel = _individual(owner)

    response = _post(client, owner, channel.pk, {"amount": "nope"})

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_unknown_and_inaccessible_channels_share_access_denied(client: Client) -> None:
    owner = _user("owner")
    stranger = _user("stranger")
    channel = _individual(owner)
    customer = _user("customer")
    bad_body = {"amount": "nope"}

    unknown = _post(client, owner, uuid.uuid4(), bad_body)
    inaccessible = _post(client, stranger, channel.pk, _body(customer, amount="nope"))

    assert unknown.status_code == 403
    assert inaccessible.status_code == 403
    assert unknown.json() == {"code": "partner_access_denied"}
    assert inaccessible.json() == unknown.json()
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_viewer_is_forbidden_before_the_body(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    channel = _team(owner, "Team")
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)

    response = _post(client, viewer, channel.pk, {"amount": "nope"})

    assert response.status_code == 403
    assert response.json() == {"code": "partner_grant_forbidden"}
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_member_suspended_and_revoked_are_access_denied(client: Client) -> None:
    owner = _user("owner")
    member = _user("member")
    admin = _user("admin")
    channel = _team(owner, "Team")
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    Membership.objects.filter(user=owner).update(status=MembershipStatus.SUSPENDED)
    Membership.objects.filter(user=admin).update(status=MembershipStatus.REVOKED)

    denied = [
        _post(client, member, channel.pk, {"amount": "nope"}),
        _post(client, owner, channel.pk, {"customer_id": 1}),
        _post(client, admin, channel.pk, {"amount": "1.000000"}),
    ]

    assert [response.status_code for response in denied] == [403, 403, 403]
    assert [response.json() for response in denied] == [
        {"code": "partner_access_denied"},
        {"code": "partner_access_denied"},
        {"code": "partner_access_denied"},
    ]


@ENABLED
@pytest.mark.django_db
def test_individual_owner_grants_the_full_personal_balance(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _individual(owner)
    _attribute(customer, channel)
    personal = owner.billing_account
    _credit(personal, "10.000000", LedgerReferenceType.DEPOSIT)
    _credit(personal, "5.000000", LedgerReferenceType.PARTNER_MARGIN)
    org_accounts_before = Account.objects.filter(kind=AccountKind.ORGANIZATION).count()
    attribution_id = CustomerAttribution.objects.get(user=customer).pk

    response = _post(client, owner, channel.pk, _body(customer, amount="15.000000"))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response
    grant = PartnerCreditGrant.objects.get()
    assert response.json() == {
        "grant_id": str(grant.pk),
        "customer_id": customer.pk,
        "amount": "15.000000",
        "created_at": grant.created_at.isoformat().replace("+00:00", "Z"),
    }
    personal.refresh_from_db()
    customer.billing_account.refresh_from_db()
    assert personal.balance == Decimal("0.000000")
    assert personal.kind == AccountKind.PERSONAL
    assert customer.billing_account.balance == Decimal("15.000000")
    assert customer.billing_account.kind == AccountKind.PERSONAL
    assert grant.debit_ledger_entry.account_id == personal.pk
    assert grant.debit_ledger_entry.delta == Decimal("-15.000000")
    assert grant.credit_ledger_entry.account_id == customer.billing_account.pk
    assert grant.credit_ledger_entry.delta == Decimal("15.000000")
    assert _grant_rows() == 2
    assert CustomerAttribution.objects.get(pk=attribution_id).partner_channel_id == (
        channel.pk
    )
    assert Account.objects.filter(kind=AccountKind.ORGANIZATION).count() == (
        org_accounts_before
    )
    assert channel.owner_user_id == owner.pk


@ENABLED
@pytest.mark.django_db
def test_owner_cannot_grant_to_the_same_personal_account(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    attribution = _attribute(owner, channel)
    personal = owner.billing_account
    _credit(personal, "20.000000", LedgerReferenceType.ADMIN_ADJUSTMENT)

    response = _post(client, owner, channel.pk, _body(owner))

    assert response.status_code == 409
    assert response.json() == {"code": "partner_grant_same_account"}
    personal.refresh_from_db()
    attribution.refresh_from_db()
    assert personal.balance == Decimal("20.000000")
    assert attribution.partner_channel_id == channel.pk
    assert PartnerCreditGrant.objects.count() == 0
    assert _grant_rows() == 0


@ENABLED
@pytest.mark.django_db
def test_customer_of_another_channel_is_rejected(client: Client) -> None:
    owner = _user("owner")
    other_owner = _user("other")
    customer = _user("customer")
    channel = _individual(owner)
    other = _individual(other_owner)
    _attribute(customer, other)
    personal = owner.billing_account
    _credit(personal, "20.000000", LedgerReferenceType.DEPOSIT)

    response = _post(client, owner, channel.pk, _body(customer))

    assert response.status_code == 409
    assert response.json() == {"code": "customer_attribution_changed"}
    personal.refresh_from_db()
    assert personal.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_history_and_pending_are_not_enough(client: Client) -> None:
    owner = _user("owner")
    moved = _user("moved")
    pending_user = _user("pending")
    elsewhere_owner = _user("elsewhere")
    channel = _individual(owner)
    elsewhere = _individual(elsewhere_owner)
    _credit(owner.billing_account, "20.000000", LedgerReferenceType.DEPOSIT)
    CustomerAttributionHistory.objects.create(
        user=moved,
        from_partner_channel=channel,
        to_partner_channel=elsewhere,
        changed_by=owner,
        changed_by_user_id_snapshot=owner.pk,
        change_reason="transfer",
        changed_at=timezone.now(),
        previous_attributed_at=timezone.now(),
    )
    PendingPartnerAttribution.objects.create(
        user=pending_user,
        partner_channel=channel,
        invite_token_snapshot="token",
        expires_at=timezone.now() + timedelta(hours=1),
    )

    history = _post(client, owner, channel.pk, _body(moved, key="history"))
    pending = _post(client, owner, channel.pk, _body(pending_user, key="pending"))

    assert history.status_code == 404
    assert history.json() == {"code": "customer_not_found"}
    assert pending.status_code == 404
    assert pending.json() == {"code": "customer_not_found"}
    assert PartnerCreditGrant.objects.count() == 0
    assert _grant_rows() == 0


@ENABLED
@pytest.mark.django_db
def test_insufficient_grant_does_not_leave_a_destination_account(
    client: Client,
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _individual(owner)
    _attribute(customer, channel)
    Account.objects.filter(user=customer).delete()
    customer.refresh_from_db()
    assert not Account.objects.filter(user=customer).exists()
    personal = owner.billing_account
    balance_before = personal.balance

    response = _post(client, owner, channel.pk, _body(customer))

    assert response.status_code == 409
    assert response.json() == {"code": "insufficient_funds"}
    personal.refresh_from_db()
    assert personal.balance == balance_before
    assert not Account.objects.filter(user=customer).exists()
    assert PartnerCreditGrant.objects.count() == 0
    assert _grant_rows() == 0


@ENABLED
@pytest.mark.django_db
def test_replay_returns_the_same_grant_after_attribution_moves(client: Client) -> None:
    owner = _user("owner")
    other_owner = _user("other")
    customer = _user("customer")
    channel = _team(owner, "Team")
    other = _team(other_owner, "Other")
    attribution = _attribute(customer, channel)
    team = channel.organization.account
    _credit(team, "20.000000", LedgerReferenceType.ADMIN_ADJUSTMENT)

    first = _post(client, owner, channel.pk, _body(customer))
    attribution.partner_channel = other
    attribution.save(update_fields=["partner_channel"])
    second = _post(client, owner, channel.pk, _body(customer))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    team.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.count() == 1
    attribution.refresh_from_db()
    assert attribution.partner_channel_id == other.pk


@ENABLED
@pytest.mark.django_db
def test_team_owner_and_admin_debit_the_organization_account(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    first = _user("first")
    second = _user("second")
    channel = _team(owner, "Team", active=False)
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _attribute(first, channel)
    _attribute(second, channel)
    team = channel.organization.account
    _credit(team, "30.000000", LedgerReferenceType.ADMIN_ADJUSTMENT)
    personal_before = owner.billing_account.balance

    owner_response = _post(client, owner, channel.pk, _body(first, key="owner"))
    admin_response = _post(client, admin, channel.pk, _body(second, key="admin"))

    assert owner_response.status_code == 200
    assert admin_response.status_code == 200
    team.refresh_from_db()
    owner.billing_account.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert team.kind == AccountKind.ORGANIZATION
    assert owner.billing_account.balance == personal_before
    assert PartnerCreditGrant.objects.count() == 2


@ENABLED
@pytest.mark.django_db
def test_each_context_debits_only_its_own_settlement_account(client: Client) -> None:
    owner = _user("owner")
    individual = _individual(owner)
    team = _team(owner, "Team")
    individual_customer = _user("individual-customer")
    team_customer = _user("team-customer")
    _attribute(individual_customer, individual)
    _attribute(team_customer, team)
    personal = owner.billing_account
    organization = team.organization.account
    _credit(personal, "30.000000", LedgerReferenceType.DEPOSIT)
    _credit(organization, "40.000000", LedgerReferenceType.ADMIN_ADJUSTMENT)

    from_individual = _post(
        client, owner, individual.pk, _body(individual_customer, key="individual")
    )
    from_team = _post(client, owner, team.pk, _body(team_customer, key="team"))

    assert from_individual.status_code == 200
    assert from_team.status_code == 200
    personal.refresh_from_db()
    organization.refresh_from_db()
    assert personal.balance == Decimal("20.000000")
    assert organization.balance == Decimal("30.000000")
    individual_grant = PartnerCreditGrant.objects.get(idempotency_key="individual")
    team_grant = PartnerCreditGrant.objects.get(idempotency_key="team")
    assert individual_grant.debit_ledger_entry.account_id == personal.pk
    assert team_grant.debit_ledger_entry.account_id == organization.pk


@ENABLED
@pytest.mark.django_db
def test_two_team_channels_do_not_fall_back(client: Client) -> None:
    owner = _user("owner")
    first = _team(owner, "First")
    second_org = create_organization(name="Second", actor=_user("second-owner"))
    second = PartnerChannel.objects.create(
        organization=second_org,
        revenue_share_percent=Decimal("50.00"),
    )
    _membership(user=owner, channel=second, role=MembershipRole.ADMIN)
    first_customer = _user("first-customer")
    second_customer = _user("second-customer")
    _attribute(first_customer, first)
    _attribute(second_customer, second)
    _credit(
        second.organization.account, "20.000000", LedgerReferenceType.ADMIN_ADJUSTMENT
    )
    first_account = first.organization.account

    response = _post(client, owner, first.pk, _body(first_customer))
    crossed = _post(client, owner, first.pk, _body(second_customer, key="other"))

    assert response.status_code == 409
    assert response.json() == {"code": "insufficient_funds"}
    assert crossed.status_code == 409
    assert crossed.json() == {"code": "customer_attribution_changed"}
    first_account.refresh_from_db()
    second.organization.account.refresh_from_db()
    assert first_account.balance == Decimal("0.000000")
    assert second.organization.account.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_missing_settlement_account_is_a_server_error(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _individual(owner)
    _attribute(customer, channel)
    Account.objects.filter(user=owner).delete()
    caplog.set_level("ERROR")

    response = _post(client, owner, channel.pk, _body(customer))

    assert response.status_code == 500
    assert response.json() == {"code": "partner_settlement_account_missing"}
    assert "partner_channel_grant.settlement_invalid" in caplog.text
    assert "error_type=PartnerSettlementAccountMissing" in caplog.text
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_legacy_grant_still_refuses_an_individual_only_partner(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    _individual(owner)
    _attribute(customer, owner.individual_partner_channel)
    _credit(owner.billing_account, "20.000000", LedgerReferenceType.DEPOSIT)

    response = _legacy(client, owner, _body(customer))

    assert response.status_code == 403
    assert response.json() == {"code": "partner_access_denied"}
    owner.billing_account.refresh_from_db()
    assert owner.billing_account.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_same_key_with_a_different_amount_conflicts(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _individual(owner)
    _attribute(customer, channel)
    _credit(owner.billing_account, "20.000000", LedgerReferenceType.DEPOSIT)
    _post(client, owner, channel.pk, _body(customer, amount="10.000000"))

    response = _post(client, owner, channel.pk, _body(customer, amount="4.000000"))

    assert response.status_code == 409
    assert response.json() == {"code": "idempotency_key_conflict"}
    owner.billing_account.refresh_from_db()
    assert owner.billing_account.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.count() == 1
