"""Settlement Account resolution (ADR 024). No money movement."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.billing.models import Account, AccountKind, CreditLedgerEntry
from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.billing.services.partner_settlement import (
    PartnerChannelOwnershipInvalid,
    PartnerSettlementAccountMissing,
    resolve_partner_settlement_account,
)
from apps.organizations.models import Membership
from apps.organizations.services.account_binding import create_organization

pytestmark = pytest.mark.django_db


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="secret123",
    )


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


def _snapshot() -> tuple:
    return (
        list(
            Account.objects.order_by("id").values_list(
                "id",
                "kind",
                "user_id",
                "balance",
                "version",
            )
        ),
        CreditLedgerEntry.objects.count(),
        CustomerAttribution.objects.count(),
        list(
            Membership.objects.order_by("id").values_list(
                "id",
                "user_id",
                "organization_id",
                "role",
                "status",
            )
        ),
        list(
            PartnerChannel.objects.order_by("id").values_list(
                "id",
                "kind",
                "owner_user_id",
                "organization_id",
                "is_active",
                "revenue_share_percent",
            )
        ),
    )


def test_individual_channel_resolves_owner_personal_account() -> None:
    owner = _user("owner")
    channel = _individual(owner)
    personal = owner.billing_account
    before = _snapshot()

    account = resolve_partner_settlement_account(channel)

    assert account.pk == personal.pk
    assert account.kind == AccountKind.PERSONAL
    assert account.user_id == owner.pk
    assert _snapshot() == before


def test_team_channel_resolves_organization_account() -> None:
    actor = _user("actor")
    channel = _team(actor, "Fleet")
    team_account = channel.organization.account
    before = _snapshot()

    account = resolve_partner_settlement_account(channel)

    assert account.pk == team_account.pk
    assert account.kind == AccountKind.ORGANIZATION
    assert account.user_id is None
    assert account.organization.pk == channel.organization_id
    assert _snapshot() == before


def test_partner_channel_has_no_account_field() -> None:
    names = {field.name for field in PartnerChannel._meta.fields}
    assert "account" not in names
    assert "account_id" not in {field.attname for field in PartnerChannel._meta.fields}


def test_missing_personal_account_raises_settlement_error() -> None:
    owner = _user("owner")
    personal = Account.objects.get(user=owner)
    Account.objects.filter(pk=personal.pk).delete()
    channel = PartnerChannel.objects.select_related("owner_user").get(
        pk=_individual(owner).pk
    )
    assert not Account.objects.filter(pk=personal.pk).exists()
    before_accounts = Account.objects.count()
    before_ledger = CreditLedgerEntry.objects.count()

    with pytest.raises(PartnerSettlementAccountMissing, match="no personal") as raised:
        resolve_partner_settlement_account(channel)

    assert not isinstance(raised.value, Account.DoesNotExist)
    assert Account.objects.filter(user=owner).count() == 0
    assert Account.objects.count() == before_accounts
    assert CreditLedgerEntry.objects.count() == before_ledger


def test_missing_organization_account_relation_raises_settlement_error() -> None:
    actor = _user("actor")
    channel = _team(actor, "Fleet")
    organization = channel.organization
    original_account_id = organization.account_id
    organization.account_id = None
    organization._state.fields_cache.pop("account", None)
    before = _snapshot()

    with pytest.raises(
        PartnerSettlementAccountMissing,
        match="no organization",
    ) as raised:
        resolve_partner_settlement_account(channel)

    assert not isinstance(raised.value, Account.DoesNotExist)
    organization.refresh_from_db()
    assert organization.account_id == original_account_id
    assert _snapshot() == before


def test_dangling_organization_account_id_raises_settlement_error() -> None:
    actor = _user("actor")
    channel = _team(actor, "Fleet")
    organization = channel.organization
    original_account_id = organization.account_id
    organization.account_id = uuid.uuid4()
    organization._state.fields_cache.pop("account", None)

    with pytest.raises(PartnerSettlementAccountMissing, match="no organization"):
        resolve_partner_settlement_account(channel)

    organization.refresh_from_db()
    assert organization.account_id == original_account_id


def test_personal_account_linked_to_an_organization_is_rejected() -> None:
    owner = _user("owner")
    channel = _individual(owner)
    personal = owner.billing_account
    org = create_organization(name="Other", actor=_user("other"))
    org.account = personal
    org.save(update_fields=["account"])
    before = _snapshot()

    with pytest.raises(PartnerSettlementAccountMissing, match="does not belong"):
        resolve_partner_settlement_account(channel)

    personal.refresh_from_db()
    assert personal.kind == AccountKind.PERSONAL
    assert personal.user_id == owner.pk
    assert _snapshot() == before


def test_team_account_that_is_personal_is_rejected() -> None:
    actor = _user("actor")
    channel = _team(actor, "Fleet")
    personal = actor.billing_account
    organization = channel.organization
    organization.account = personal
    organization.save(update_fields=["account"])
    before_ledger = CreditLedgerEntry.objects.count()

    with pytest.raises(PartnerSettlementAccountMissing, match="does not belong"):
        resolve_partner_settlement_account(channel)

    personal.refresh_from_db()
    assert personal.kind == AccountKind.PERSONAL
    assert personal.balance == Decimal("0")
    assert CreditLedgerEntry.objects.count() == before_ledger


def test_cached_account_owned_by_another_user_is_rejected() -> None:
    owner = _user("owner")
    other = _user("other")
    channel = _individual(owner)
    other_account = other.billing_account
    other_user_id = other_account.user_id
    owner._state.fields_cache["billing_account"] = other_account

    with pytest.raises(PartnerSettlementAccountMissing, match="does not belong"):
        resolve_partner_settlement_account(channel)

    assert other_account.user_id == other_user_id


def test_cached_account_from_another_organization_is_rejected() -> None:
    actor = _user("actor")
    channel = _team(actor, "Fleet")
    other = _team(_user("other"), "Other")
    foreign_account = other.organization.account
    organization = channel.organization
    organization._state.fields_cache["account"] = foreign_account

    with pytest.raises(PartnerSettlementAccountMissing, match="does not belong"):
        resolve_partner_settlement_account(channel)

    organization.refresh_from_db()
    assert organization.account_id != foreign_account.pk


def test_broken_owner_shape_raises_before_account_lookup() -> None:
    owner = _user("owner")
    org = create_organization(name="Fleet", actor=owner)
    broken = PartnerChannel(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=None,
        organization=org,
        revenue_share_percent=Decimal("10.00"),
    )
    before = Account.objects.count()

    with pytest.raises(PartnerChannelOwnershipInvalid, match="Individual"):
        resolve_partner_settlement_account(broken)

    both = PartnerChannel(
        kind=PartnerChannel.Kind.TEAM,
        owner_user=owner,
        organization=org,
        revenue_share_percent=Decimal("10.00"),
    )
    with pytest.raises(PartnerChannelOwnershipInvalid, match="Team"):
        resolve_partner_settlement_account(both)
    unknown = PartnerChannel(
        kind="neither",
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("10.00"),
    )
    with pytest.raises(PartnerChannelOwnershipInvalid, match="kind"):
        resolve_partner_settlement_account(unknown)
    assert Account.objects.count() == before


def test_resolver_does_not_lock_or_scan_the_ledger() -> None:
    owner = _user("owner")
    channel = PartnerChannel.objects.select_related("owner_user").get(
        pk=_individual(owner).pk
    )

    with CaptureQueriesContext(connection) as ctx:
        resolve_partner_settlement_account(channel)

    sql = " ".join(query["sql"] for query in ctx.captured_queries).lower()
    assert "for update" not in sql
    assert "creditledger" not in sql
    assert "partnermargin" not in sql
    assert len(ctx.captured_queries) == 2
