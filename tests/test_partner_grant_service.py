"""PartnerGrantService (ADR 023). No HTTP."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.exceptions import InsufficientFundsError, InvalidAmountError
from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_grant import (
    CustomerAttributionChanged,
    CustomerNotAttributed,
    PartnerChannelDisabled,
    PartnerGrantForbidden,
    PartnerGrantIdempotencyConflict,
    PartnerGrantNegativeBalance,
    partner_grant_service,
)
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


def _user(prefix: str = "user") -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="secret123",
    )


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


def _fund_team(channel: PartnerChannel, amount: str = "20.000000") -> Account:
    account = channel.organization.account
    credit_service.credit(
        account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"fund-{uuid.uuid4()}",
        idempotency_key=f"fund-{uuid.uuid4()}",
    )
    return account


def _grant(
    *,
    actor: User,
    channel: PartnerChannel,
    customer: User,
    amount: str = "10.000000",
    key: str | None = None,
) -> PartnerCreditGrant:
    return partner_grant_service.grant(
        actor=actor,
        partner_channel=channel,
        customer=customer,
        amount=Decimal(amount),
        idempotency_key=key or f"grant-{uuid.uuid4()}",
    )


@ENABLED
@pytest.mark.django_db
def test_grant_moves_team_balance_to_the_customer_once() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)

    grant = _grant(actor=owner, channel=channel, customer=customer, key="grant-1")

    assert grant.partner_channel_id == channel.pk
    assert grant.customer_user_id == customer.pk
    assert grant.customer_user_id_snapshot == customer.pk
    assert grant.granted_by_id == owner.pk
    assert grant.granted_by_user_id_snapshot == owner.pk
    assert grant.amount == Decimal("10.000000")
    assert grant.idempotency_key == "grant-1"
    debit = grant.debit_ledger_entry
    credit = grant.credit_ledger_entry
    assert debit.reference_type == LedgerReferenceType.PARTNER_GRANT_OUT
    assert credit.reference_type == LedgerReferenceType.PARTNER_GRANT_IN
    assert debit.reference_id == str(grant.pk)
    assert credit.reference_id == str(grant.pk)
    assert debit.idempotency_key == f"partner-grant-out:{grant.pk}"
    assert credit.idempotency_key == f"partner-grant-in:{grant.pk}"
    assert debit.idempotency_key != "grant-1"
    assert debit.delta == Decimal("-10.000000")
    assert credit.delta == Decimal("10.000000")
    assert debit.account_id == team.pk
    assert credit.account_id == customer.billing_account.pk
    team.refresh_from_db()
    customer.billing_account.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert customer.billing_account.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type__in=[
                LedgerReferenceType.PARTNER_GRANT_OUT,
                LedgerReferenceType.PARTNER_GRANT_IN,
            ]
        ).count()
        == 2
    )


@ENABLED
@pytest.mark.django_db
def test_admin_may_grant() -> None:
    owner = _user("owner")
    admin = _user("admin")
    customer = _user("customer")
    channel = _channel_for(owner)
    Membership.objects.create(
        organization=channel.organization,
        user=admin,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.ACTIVE,
    )
    _attribute(customer, channel)
    _fund_team(channel)

    grant = _grant(actor=admin, channel=channel, customer=customer)

    assert grant.granted_by_id == admin.pk


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "role",
    [MembershipRole.VIEWER, MembershipRole.MEMBER],
)
def test_viewer_and_member_cannot_grant(role: str) -> None:
    owner = _user("owner")
    actor = _user("actor")
    customer = _user("customer")
    channel = _channel_for(owner)
    Membership.objects.create(
        organization=channel.organization,
        user=actor,
        role=role,
        status=MembershipStatus.ACTIVE,
    )
    _attribute(customer, channel)
    team = _fund_team(channel)

    with pytest.raises(PartnerGrantForbidden):
        _grant(actor=actor, channel=channel, customer=customer)

    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_suspended_owner_cannot_grant() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    Membership.objects.filter(user=owner).update(status=MembershipStatus.SUSPENDED)
    _attribute(customer, channel)
    _fund_team(channel)

    with pytest.raises(PartnerGrantForbidden):
        _grant(actor=owner, channel=channel, customer=customer)

    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_grants_existing_balance() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner, active=False)
    _attribute(customer, channel)
    _fund_team(channel)

    grant = _grant(actor=owner, channel=channel, customer=customer)

    assert grant.amount == Decimal("10.000000")


@pytest.mark.django_db
@override_settings(PARTNER_CHANNEL_ENABLED=False, BILLING_ENABLED=True)
def test_flag_off_writes_nothing() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)

    with pytest.raises(PartnerChannelDisabled):
        _grant(actor=owner, channel=channel, customer=customer)

    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_replay_returns_the_same_grant_without_a_second_move() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)

    first = _grant(actor=owner, channel=channel, customer=customer, key="same")
    second = _grant(actor=owner, channel=channel, customer=customer, key="same")

    assert second.pk == first.pk
    assert second.debit_ledger_entry_id == first.debit_ledger_entry_id
    assert second.credit_ledger_entry_id == first.credit_ledger_entry_id
    team.refresh_from_db()
    customer.billing_account.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert customer.billing_account.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.count() == 1


@ENABLED
@pytest.mark.django_db
def test_same_key_with_a_different_amount_conflicts() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)
    _grant(
        actor=owner, channel=channel, customer=customer, amount="10.000000", key="same"
    )

    with pytest.raises(PartnerGrantIdempotencyConflict):
        _grant(
            actor=owner,
            channel=channel,
            customer=customer,
            amount="4.000000",
            key="same",
        )

    team.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.count() == 1


@ENABLED
@pytest.mark.django_db
def test_same_key_with_a_different_customer_conflicts() -> None:
    owner = _user("owner")
    first = _user("first")
    second = _user("second")
    channel = _channel_for(owner)
    _attribute(first, channel)
    _attribute(second, channel)
    team = _fund_team(channel)
    _grant(actor=owner, channel=channel, customer=first, key="same")

    with pytest.raises(PartnerGrantIdempotencyConflict):
        _grant(actor=owner, channel=channel, customer=second, key="same")

    team.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_GRANT_IN
        ).count()
        == 1
    )


@ENABLED
@pytest.mark.django_db
def test_missing_attribution_writes_nothing() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    team = _fund_team(channel)

    with pytest.raises(CustomerNotAttributed):
        _grant(actor=owner, channel=channel, customer=customer)

    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_GRANT_OUT
    ).exists()


@ENABLED
@pytest.mark.django_db
def test_attribution_on_another_channel_writes_nothing() -> None:
    owner = _user("owner")
    other_owner = _user("other-owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    other = _channel_for(other_owner)
    _attribute(customer, other)
    team = _fund_team(channel)

    with pytest.raises(CustomerAttributionChanged):
        _grant(actor=owner, channel=channel, customer=customer)

    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_insufficient_funds_rolls_back() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel, "5.000000")

    with pytest.raises(InsufficientFundsError):
        _grant(actor=owner, channel=channel, customer=customer, amount="10.000000")

    team.refresh_from_db()
    assert team.balance == Decimal("5.000000")
    assert PartnerCreditGrant.objects.count() == 0
    assert customer.billing_account.balance == Decimal("0")


@ENABLED
@pytest.mark.django_db
def test_negative_team_balance_is_not_rewritten(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The balance check constraint still forbids a stored negative cache."""
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)
    queryset_cls = type(Account.objects.all())
    real_get = queryset_cls.get

    def get_negative_team(self, *args, **kwargs):
        row = real_get(self, *args, **kwargs)
        if row.pk == team.pk:
            row.balance = Decimal("-1.000000")
        return row

    monkeypatch.setattr(queryset_cls, "get", get_negative_team)
    caplog.set_level("ERROR")

    with pytest.raises(PartnerGrantNegativeBalance):
        _grant(actor=owner, channel=channel, customer=customer)

    monkeypatch.undo()
    team.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert PartnerCreditGrant.objects.count() == 0
    assert "partner_grant.negative_balance" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["0", "-1.000000"])
def test_non_positive_amount_is_rejected(amount: str) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    _fund_team(channel)

    with pytest.raises(InvalidAmountError):
        _grant(actor=owner, channel=channel, customer=customer, amount=amount)

    assert PartnerCreditGrant.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_insert_integrity_error_rolls_back_both_legs(monkeypatch) -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)

    def _boom(*args, **kwargs):
        raise IntegrityError("grant insert lost the race")

    monkeypatch.setattr(PartnerCreditGrant.objects, "create", _boom)
    with pytest.raises(IntegrityError):
        _grant(actor=owner, channel=channel, customer=customer)

    team.refresh_from_db()
    customer.billing_account.refresh_from_db()
    assert team.balance == Decimal("20.000000")
    assert customer.billing_account.balance == Decimal("0")
    assert PartnerCreditGrant.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type__in=[
            LedgerReferenceType.PARTNER_GRANT_OUT,
            LedgerReferenceType.PARTNER_GRANT_IN,
        ]
    ).exists()


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_concurrent_same_key_creates_one_grant() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel_for(owner)
    _attribute(customer, channel)
    team = _fund_team(channel)
    customer_account_id = customer.billing_account.pk

    def _once() -> str:
        try:
            grant = partner_grant_service.grant(
                actor=owner,
                partner_channel=channel,
                customer=customer,
                amount=Decimal("10.000000"),
                idempotency_key="race-same",
            )
            return str(grant.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        grant_ids = list(pool.map(lambda _: _once(), range(2)))

    assert grant_ids[0] == grant_ids[1]
    team.refresh_from_db()
    personal = Account.objects.get(pk=customer_account_id)
    assert team.balance == Decimal("10.000000")
    assert personal.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.filter(idempotency_key="race-same").count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_GRANT_OUT
        ).count()
        == 1
    )


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_concurrent_same_key_different_customer_moves_money_once() -> None:
    owner = _user("owner")
    first = _user("first")
    second = _user("second")
    channel = _channel_for(owner)
    _attribute(first, channel)
    _attribute(second, channel)
    team = _fund_team(channel)

    def _once(customer: User) -> str:
        try:
            partner_grant_service.grant(
                actor=owner,
                partner_channel=channel,
                customer=customer,
                amount=Decimal("10.000000"),
                idempotency_key="race-split",
            )
            return "ok"
        except PartnerGrantIdempotencyConflict:
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_once, [first, second]))

    assert sorted(results) == ["conflict", "ok"]
    team.refresh_from_db()
    assert team.balance == Decimal("10.000000")
    assert PartnerCreditGrant.objects.filter(idempotency_key="race-split").count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_GRANT_OUT
        ).count()
        == 1
    )
