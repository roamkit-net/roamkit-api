"""PartnerGrantsService (ADR 023). Read-only. No HTTP."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_grants import (
    PartnerGrantsQuery,
    partner_grants_service,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@Example.com",
        password="secret123",
    )


def _channel_for(actor: User) -> PartnerChannel:
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=True,
    )


def _attribute(customer: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


def _ledger(channel: PartnerChannel):
    return credit_service.credit(
        channel.organization.account,
        Decimal("0.100000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"grant-{uuid.uuid4()}",
        idempotency_key=f"grant-{uuid.uuid4()}",
    )


def _grant(
    channel: PartnerChannel,
    attribution: CustomerAttribution,
    *,
    amount: str,
    snapshot_user_id: int | None = None,
    customer: User | None = None,
    granted_by: User | None = None,
    granted_by_snapshot: int = 1,
) -> PartnerCreditGrant:
    return PartnerCreditGrant.objects.create(
        partner_channel=channel,
        customer_user=customer,
        customer_user_id_snapshot=(
            attribution.user_id if snapshot_user_id is None else snapshot_user_id
        ),
        customer_attribution=attribution,
        granted_by=granted_by,
        granted_by_user_id_snapshot=granted_by_snapshot,
        amount=Decimal(amount),
        idempotency_key=f"idem-{uuid.uuid4()}",
        debit_ledger_entry=_ledger(channel),
        credit_ledger_entry=_ledger(channel),
    )


def _query(**overrides: object) -> PartnerGrantsQuery:
    values: dict[str, object] = {
        "page": 1,
        "page_size": 50,
        "sort": "created_at",
        "order": "desc",
    }
    values.update(overrides)
    return PartnerGrantsQuery(**values)  # type: ignore[arg-type]


@ENABLED
@pytest.mark.django_db
def test_history_uses_snapshot_customer_id_and_live_email_only_for_display() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    other = _channel_for(_user("other-owner"))
    customer = _user("ada")
    attribution = _attribute(customer, channel)
    kept = _grant(
        channel,
        attribution,
        amount="2.500000",
        customer=customer,
        granted_by=owner,
        granted_by_snapshot=owner.pk,
    )
    deleted_customer = _grant(
        channel,
        attribution,
        amount="1.000000",
        snapshot_user_id=customer.pk,
        customer=None,
        granted_by=None,
        granted_by_snapshot=99,
    )
    _grant(
        other,
        _attribute(_user("bea"), other),
        amount="9.000000",
        granted_by=None,
    )

    page = partner_grants_service.list_grants(channel, _query())

    assert page.count == 2
    by_id = {row.grant_id: row for row in page.results}
    assert by_id[kept.id].customer_id == customer.pk
    assert by_id[kept.id].email == customer.email
    assert by_id[kept.id].display_name == ""
    assert by_id[kept.id].amount == Decimal("2.500000")
    assert by_id[kept.id].granted_by is not None
    assert by_id[kept.id].granted_by.user_id == owner.pk
    assert by_id[kept.id].granted_by.email == owner.email
    assert by_id[kept.id].granted_by.display_name == ""
    assert by_id[deleted_customer.id].customer_id == customer.pk
    assert by_id[deleted_customer.id].email is None
    assert by_id[deleted_customer.id].display_name == ""
    assert by_id[deleted_customer.id].granted_by is None


@ENABLED
@pytest.mark.django_db
def test_equal_amounts_break_ties_by_grant_id_ascending() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    customer = _user("ada")
    attribution = _attribute(customer, channel)
    first = _grant(channel, attribution, amount="3.000000", customer=customer)
    second = _grant(channel, attribution, amount="3.000000", customer=customer)

    page = partner_grants_service.list_grants(
        channel, _query(sort="amount", order="desc")
    )

    assert [row.grant_id for row in page.results] == sorted((first.id, second.id))


@ENABLED
@pytest.mark.django_db
def test_count_is_before_pagination_and_a_late_page_is_empty() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    customer = _user("ada")
    attribution = _attribute(customer, channel)
    for amount in ("1.000000", "2.000000", "3.000000"):
        _grant(channel, attribution, amount=amount, customer=customer)

    page = partner_grants_service.list_grants(
        channel, _query(page=2, page_size=2, sort="amount", order="asc")
    )
    past = partner_grants_service.list_grants(
        channel, _query(page=4, page_size=2, sort="amount", order="asc")
    )

    assert page.count == 3
    assert [row.amount for row in page.results] == [Decimal("3.000000")]
    assert past.count == 3
    assert past.page == 4
    assert past.results == ()


@ENABLED
@pytest.mark.django_db
def test_list_uses_a_count_and_one_page_query() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    customer = _user("ada")
    attribution = _attribute(customer, channel)
    for amount in ("1.000000", "2.000000"):
        _grant(
            channel,
            attribution,
            amount=amount,
            customer=customer,
            granted_by=owner,
            granted_by_snapshot=owner.pk,
        )

    with CaptureQueriesContext(connection) as captured:
        page = partner_grants_service.list_grants(channel, _query())

    assert page.count == 2
    assert len(captured) == 2
