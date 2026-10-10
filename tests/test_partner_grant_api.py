"""POST /api/v1/billing/partner-grants/ (ADR 023)."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
)
from apps.billing.services.credit import credit_service
from apps.billing.throttles import PartnerGrantRateThrottle
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
URL = "/api/v1/billing/partner-grants/"
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


def _attribute(customer: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


def _fund(channel: PartnerChannel, amount: str = "20.000000") -> Account:
    account = channel.organization.account
    credit_service.credit(
        account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"fund-{uuid.uuid4()}",
        idempotency_key=f"fund-{uuid.uuid4()}",
    )
    return account


def _post(
    client: Client,
    user: User | None,
    body: object,
    *,
    raw: str | None = None,
    request_id: str = "req-grant-1",
) -> object:
    headers = _auth(user) if user is not None else {}
    payload = raw if raw is not None else json.dumps(body)
    return client.post(
        URL,
        data=payload,
        content_type="application/json",
        HTTP_X_REQUEST_ID=request_id,
        **headers,
    )


def _body(customer: User, amount: str = "10.000000", key: str = "grant-1") -> dict:
    return {
        "customer_id": customer.pk,
        "amount": amount,
        "idempotency_key": key,
    }


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required_before_the_body(client: Client) -> None:
    response = _post(client, None, {"amount": "nope"})

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"


@ENABLED
@pytest.mark.django_db
def test_flag_off_is_disabled_before_membership(client: Client, settings) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    customer = _user("customer")
    _channel_for(owner)

    response = _post(client, owner, _body(customer))

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_grant_returns_the_locked_body_and_moves_money_once(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund(channel)
    caplog.set_level("INFO")

    response = _post(client, owner, _body(customer, key="  spaced  "))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    grant = PartnerCreditGrant.objects.get()
    assert response.json() == {
        "grant_id": str(grant.pk),
        "customer_id": customer.pk,
        "amount": "10.000000",
        "created_at": grant.created_at.isoformat().replace("+00:00", "Z"),
    }
    assert isinstance(response.json()["customer_id"], int)
    assert grant.idempotency_key == "  spaced  "
    team.refresh_from_db()
    customer.billing_account.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert customer.billing_account.balance == Decimal("10.000000")
    assert "partner.grant.succeeded" in caplog.text
    assert "request_id=req-grant-1" in caplog.text
    assert f"customer_user_id={customer.pk}" in caplog.text
    assert "amount=" not in caplog.text
    assert customer.email not in caplog.text


@ENABLED
@pytest.mark.django_db
def test_replay_returns_the_same_grant_without_a_second_audit(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    _fund(channel)
    caplog.set_level("INFO")
    first = _post(client, owner, _body(customer))
    caplog.clear()

    second = _post(client, owner, _body(customer))

    assert second.status_code == 200
    assert second.json()["grant_id"] == first.json()["grant_id"]
    assert PartnerCreditGrant.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_GRANT_OUT
        ).count()
        == 1
    )
    assert "partner_grant.created" not in caplog.text


@ENABLED
@pytest.mark.django_db
def test_replay_still_returns_when_the_new_grant_limit_is_spent(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    _fund(channel)
    monkeypatch.setattr(PartnerGrantRateThrottle, "get_rate", lambda self: "1/min")
    cache.clear()
    headers_body = _body(customer, key="first")
    assert _post(client, owner, headers_body).status_code == 200

    blocked = _post(client, owner, _body(customer, key="second"))
    replay = _post(client, owner, headers_body)

    assert blocked.status_code == 429
    assert blocked.json() == {"code": "rate_limited"}
    assert blocked["Cache-Control"] == "no-store"
    assert replay.status_code == 200
    assert PartnerCreditGrant.objects.count() == 1


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {
                "customer_id": "not-an-int",
                "amount": "10.000000",
                "idempotency_key": "k",
            },
            "invalid_request",
        ),
        ({"customer_id": 1, "amount": "10.000000"}, "invalid_request"),
        (
            {
                "customer_id": 1,
                "amount": "10.000000",
                "idempotency_key": "k",
                "organization_id": "x",
            },
            "invalid_request",
        ),
        (
            {
                "customer_id": 1,
                "amount": "0",
                "idempotency_key": "k",
                "team_account_id": "x",
            },
            "invalid_request",
        ),
        (
            {"customer_id": 1, "amount": "0.000000", "idempotency_key": "k"},
            "invalid_amount",
        ),
        (
            {"customer_id": 1, "amount": "-1.000000", "idempotency_key": "k"},
            "invalid_amount",
        ),
        ({"customer_id": 1, "amount": 10, "idempotency_key": "k"}, "invalid_amount"),
        (
            {"customer_id": 1, "amount": "10.0000001", "idempotency_key": "k"},
            "invalid_amount",
        ),
    ],
)
def test_body_errors_are_one_code(client: Client, body: dict, code: str) -> None:
    owner = _user("owner")
    _channel_for(owner)

    response = _post(client, owner, body)

    assert response.status_code == 400
    assert response.json() == {"code": code}


@ENABLED
@pytest.mark.django_db
def test_malformed_json_is_invalid_request(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)

    response = _post(client, owner, None, raw="{")

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request"}


@ENABLED
@pytest.mark.django_db
def test_non_object_json_is_invalid_request(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)

    response = _post(client, owner, None, raw="[]")

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request"}


@ENABLED
@pytest.mark.django_db
def test_viewer_is_access_denied(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    customer = _user("customer")
    channel = _channel_for(owner)
    Membership.objects.create(
        organization=channel.organization,
        user=viewer,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.ACTIVE,
    )
    _attribute(customer, channel)
    team = _fund(channel)

    response = _post(client, viewer, _body(customer))

    assert response.status_code == 403
    assert response.json() == {"code": "partner_access_denied"}
    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")


@ENABLED
@pytest.mark.django_db
def test_two_channels_are_ambiguous_without_org_ids(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    _channel_for(owner)
    _channel_for(owner)

    response = _post(client, owner, _body(customer))

    assert response.status_code == 409
    assert response.json() == {"code": "partner_context_ambiguous"}
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_unknown_customer_is_not_found(client: Client) -> None:
    owner = _user("owner")
    _channel_for(owner)
    missing = _user("missing")
    missing_id = missing.pk
    missing.delete()

    response = _post(client, owner, _body(missing) | {"customer_id": missing_id})

    assert response.status_code == 404
    assert response.json() == {"code": "customer_not_found"}


@ENABLED
@pytest.mark.django_db
def test_customer_without_attribution_is_not_found(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    team = _fund(channel)

    response = _post(client, owner, _body(customer))

    assert response.status_code == 404
    assert response.json() == {"code": "customer_not_found"}
    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")


@ENABLED
@pytest.mark.django_db
def test_customer_on_another_channel_is_attribution_changed(client: Client) -> None:
    owner = _user("owner")
    other = _user("other")
    customer = _user("customer")
    channel = _channel_for(owner)
    other_channel = _channel_for(other)
    _attribute(customer, other_channel)
    _fund(channel)

    response = _post(client, owner, _body(customer))

    assert response.status_code == 409
    assert response.json() == {"code": "customer_attribution_changed"}


@ENABLED
@pytest.mark.django_db
def test_same_key_different_amount_conflicts(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    _fund(channel)
    assert _post(client, owner, _body(customer, amount="10.000000")).status_code == 200

    response = _post(client, owner, _body(customer, amount="4.000000"))

    assert response.status_code == 409
    assert response.json() == {"code": "idempotency_key_conflict"}
    assert PartnerCreditGrant.objects.count() == 1


@ENABLED
@pytest.mark.django_db
def test_insufficient_funds_rolls_back(client: Client) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund(channel, "5.000000")

    response = _post(client, owner, _body(customer, amount="10.000000"))

    assert response.status_code == 409
    assert response.json() == {"code": "insufficient_funds"}
    team.refresh_from_db()
    assert team.balance == Decimal("5.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_negative_balance_uses_insufficient_funds(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund(channel)
    queryset_cls = type(Account.objects.all())
    real_get = queryset_cls.get

    def get_negative_team(self, *args, **kwargs):
        row = real_get(self, *args, **kwargs)
        if row.pk == team.pk:
            row.balance = Decimal("-1.000000")
        return row

    monkeypatch.setattr(queryset_cls, "get", get_negative_team)

    response = _post(client, owner, _body(customer))

    monkeypatch.undo()
    assert response.status_code == 409
    assert response.json() == {"code": "insufficient_funds"}
    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")


@ENABLED
@pytest.mark.django_db
def test_role_change_after_resolution_is_grant_forbidden(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    _fund(channel)
    monkeypatch.setattr(
        "apps.billing.partner_grant_views.resolve_partner_grant_channel",
        lambda user: channel,
    )
    Membership.objects.filter(user=owner).update(status=MembershipStatus.SUSPENDED)

    response = _post(client, owner, _body(customer))

    assert response.status_code == 403
    assert response.json() == {"code": "partner_grant_forbidden"}
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_admin_may_grant_and_inactive_channel_still_grants(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    customer = _user("customer")
    channel = _channel_for(owner, active=False)
    Membership.objects.create(
        organization=channel.organization,
        user=admin,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.ACTIVE,
    )
    _attribute(customer, channel)
    _fund(channel)

    response = _post(client, admin, _body(customer, amount="10.5"))

    assert response.status_code == 200
    assert response.json()["amount"] == "10.500000"
    assert response.json()["customer_id"] == customer.pk
