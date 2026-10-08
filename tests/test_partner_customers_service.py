"""PartnerCustomersService (ADR 023). Read-only. No HTTP."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_customers import (
    PartnerCustomersQuery,
    partner_customers_service,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)
_START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


def _attribute(customer: User, channel: PartnerChannel, *, at: datetime) -> None:
    CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=at,
    )


def _accrual(
    channel: PartnerChannel,
    *,
    snapshot_user_id: int,
    partner_share: str,
    source_type: str = "order",
    customer: User | None = None,
) -> None:
    entry = credit_service.credit(
        channel.organization.account,
        Decimal("0.250000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"cust-{uuid.uuid4()}",
        idempotency_key=f"cust-{uuid.uuid4()}",
    )
    PartnerMarginAccrual.objects.create(
        partner_channel=channel,
        customer_user=customer,
        customer_user_id_snapshot=snapshot_user_id,
        source_type=source_type,
        source_id=f"{source_type}-{uuid.uuid4()}",
        list_price=Decimal("10.000000"),
        net_price=Decimal("4.000000"),
        margin=Decimal("6.000000"),
        revenue_share_percent=Decimal("10.00"),
        partner_share=Decimal(partner_share),
        ledger_entry=entry,
    )


def _query(**overrides: object) -> PartnerCustomersQuery:
    values: dict[str, object] = {
        "page": 1,
        "page_size": 50,
        "sort": "total_partner_earned",
        "order": "desc",
        "q": "",
    }
    values.update(overrides)
    return PartnerCustomersQuery(**values)  # type: ignore[arg-type]


@ENABLED
@pytest.mark.django_db
def test_current_customers_keep_snapshot_earnings_from_this_channel() -> None:
    owner = _user("owner")
    other_owner = _user("other-owner")
    first = _user("ada")
    second = _user("bea")
    gone = _user("gone")
    channel = _channel_for(owner)
    other = _channel_for(other_owner)
    _attribute(first, channel, at=_START)
    _attribute(second, channel, at=_START + timedelta(days=1))
    _attribute(gone, other, at=_START)
    _accrual(channel, snapshot_user_id=first.pk, partner_share="6.000000")
    _accrual(
        channel,
        snapshot_user_id=first.pk,
        partner_share="1.500000",
        source_type="legacy",
        customer=None,
    )
    _accrual(other, snapshot_user_id=first.pk, partner_share="9.000000")
    _accrual(channel, snapshot_user_id=gone.pk, partner_share="4.000000")

    page = partner_customers_service.list_customers(channel, _query())

    assert page.count == 2
    by_id = {row.customer_id: row for row in page.results}
    assert by_id[first.pk].total_partner_earned == Decimal("7.500000")
    assert by_id[first.pk].accrual_count == 2
    assert by_id[first.pk].email == f"a***@{first.email.split('@', 1)[1]}"
    assert by_id[second.pk].total_partner_earned == Decimal("0.000000")
    assert by_id[second.pk].accrual_count == 0
    assert gone.pk not in by_id


@ENABLED
@pytest.mark.django_db
def test_equal_earnings_break_ties_by_customer_id_ascending() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    later = _user("later")
    earlier = _user("earlier")
    _attribute(later, channel, at=_START)
    _attribute(earlier, channel, at=_START)
    _accrual(channel, snapshot_user_id=later.pk, partner_share="2.000000")
    _accrual(channel, snapshot_user_id=earlier.pk, partner_share="2.000000")

    page = partner_customers_service.list_customers(channel, _query(order="desc"))

    assert [row.customer_id for row in page.results] == sorted((earlier.pk, later.pk))


@ENABLED
@pytest.mark.django_db
def test_q_matches_customer_id_or_exact_email_inside_the_channel() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    other = _channel_for(_user("other-owner"))
    ada = _user("ada")
    bea = _user("bea")
    outsider = _user("outsider")
    _attribute(ada, channel, at=_START)
    _attribute(bea, channel, at=_START)
    _attribute(outsider, other, at=_START)

    by_id = partner_customers_service.list_customers(channel, _query(q=str(ada.pk)))
    by_email = partner_customers_service.list_customers(
        channel, _query(q=ada.email.upper())
    )
    prefix = partner_customers_service.list_customers(
        channel, _query(q=ada.email.split("@", 1)[0])
    )
    outside = partner_customers_service.list_customers(
        channel, _query(q=str(outsider.pk))
    )

    assert [row.customer_id for row in by_id.results] == [ada.pk]
    assert [row.customer_id for row in by_email.results] == [ada.pk]
    assert prefix.count == 0
    assert outside.count == 0
    assert outside.results == ()


@ENABLED
@pytest.mark.django_db
def test_count_is_before_pagination_and_a_late_page_is_empty() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    for index in range(3):
        _attribute(_user(f"c{index}"), channel, at=_START + timedelta(days=index))

    page = partner_customers_service.list_customers(
        channel,
        _query(page=2, page_size=2, sort="attributed_at", order="asc"),
    )
    past = partner_customers_service.list_customers(
        channel, _query(page=4, page_size=2, sort="attributed_at", order="asc")
    )

    assert page.count == 3
    assert len(page.results) == 1
    assert past.count == 3
    assert past.results == ()
    assert past.page == 4


@ENABLED
@pytest.mark.django_db
def test_list_uses_a_count_and_one_page_query() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    for index in range(3):
        customer = _user(f"c{index}")
        _attribute(customer, channel, at=_START)
        _accrual(channel, snapshot_user_id=customer.pk, partner_share="1.000000")

    with CaptureQueriesContext(connection) as captured:
        page = partner_customers_service.list_customers(channel, _query())

    assert page.count == 3
    assert len(captured) == 2
