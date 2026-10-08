"""Subscription renewal partner-margin hook (ADR 023)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType, Subscription
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
    SubscriptionRenewalCycle,
)
from apps.billing.services import credit_service, subscription_service
from apps.billing.services.partner_margin import (
    build_source_id,
    partner_margin_service,
)
from apps.billing.services.subscription import SUBSCRIPTION_PERIOD_DAYS
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(
    BILLING_ENABLED=True,
    SUBSCRIPTIONS_ENABLED=True,
    PARTNER_CHANNEL_ENABLED=True,
)


def _customer(email: str) -> User:
    return User.objects.create_user(email=email, password="secret123")


def _subscription(
    user: User, *, list_price: str, net_price: str | None
) -> Subscription:
    package = Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="Renew",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=30,
        price_usd=Decimal(list_price),
        net_price_usd=None if net_price is None else Decimal(net_price),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    esim = Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid=f"89{uuid.uuid4().int % 10**18:018d}"[:20],
        status=Esim.Status.ACTIVATED,
    )
    return Subscription.objects.create(
        account=user.billing_account,
        esim=esim,
        price_per_period=Decimal("5.000000"),
        next_billing_date=timezone.localdate() - timedelta(days=7),
        status=Subscription.Status.ACTIVE,
    )


def _fund(user: User) -> None:
    credit_service.credit(
        user.billing_account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id=f"dep-{user.pk}",
        idempotency_key=f"dep-{user.pk}",
    )


def _channel(customer: User, *, percent: str, active: bool = True) -> PartnerChannel:
    owner = _customer(f"owner-{uuid.uuid4()}@example.com")
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    channel = PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal(percent),
        is_active=active,
    )
    CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    return channel


def _renew(subscription: Subscription) -> str:
    return subscription_service.renew_one(subscription.pk)


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    SUBSCRIPTIONS_ENABLED=True,
    PARTNER_CHANNEL_ENABLED=False,
)
def test_flag_off_renews_without_accrual() -> None:
    user = _customer("flag-off@example.com")
    _fund(user)
    subscription = _subscription(user, list_price="25.00", net_price="5.00")
    _channel(user, percent="50.00")

    assert _renew(subscription) == "renewed"

    subscription.refresh_from_db()
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.RENEWED
    assert PartnerMarginAccrual.objects.count() == 0
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 0
    )
    assert subscription.next_billing_date == cycle.billing_date + timedelta(
        days=SUBSCRIPTION_PERIOD_DAYS
    )


@ENABLED
@pytest.mark.django_db
def test_no_attribution_renews_without_accrual() -> None:
    user = _customer("no-attr@example.com")
    _fund(user)
    subscription = _subscription(user, list_price="25.00", net_price="5.00")

    assert _renew(subscription) == "renewed"
    assert PartnerMarginAccrual.objects.count() == 0
    assert (
        SubscriptionRenewalCycle.objects.get(subscription=subscription).status
        == SubscriptionRenewalCycle.Status.RENEWED
    )


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_renews_without_accrual() -> None:
    user = _customer("inactive@example.com")
    _fund(user)
    subscription = _subscription(user, list_price="25.00", net_price="5.00")
    _channel(user, percent="50.00", active=False)

    assert _renew(subscription) == "renewed"
    assert PartnerMarginAccrual.objects.count() == 0


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("list_price", "net_price", "percent"),
    [
        ("25.00", None, "50.00"),
        ("25.00", "-1.00", "50.00"),
        ("5.00", "10.00", "50.00"),
        ("25.00", "5.00", "0.00"),
        ("10.00", "10.00", "50.00"),
    ],
)
def test_price_skips_still_renew(
    list_price: str, net_price: str | None, percent: str
) -> None:
    user = _customer(f"skip-{uuid.uuid4()}@example.com")
    _fund(user)
    subscription = _subscription(user, list_price=list_price, net_price=net_price)
    _channel(user, percent=percent)

    assert _renew(subscription) == "renewed"
    assert PartnerMarginAccrual.objects.count() == 0
    assert (
        SubscriptionRenewalCycle.objects.get(subscription=subscription).status
        == SubscriptionRenewalCycle.Status.RENEWED
    )


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("percent", "expected_share"),
    [
        ("50.00", "10.000000"),
        ("33.33", "6.666000"),
        ("12.50", "2.500000"),
    ],
)
def test_subscription_accrual_uses_cycle_snapshot(
    percent: str, expected_share: str
) -> None:
    user = _customer(f"earn-{uuid.uuid4()}@example.com")
    _fund(user)
    subscription = _subscription(user, list_price="25.00", net_price="5.00")
    channel = _channel(user, percent=percent)
    billing_date = subscription.next_billing_date

    assert _renew(subscription) == "renewed"

    source_id = build_source_id(
        source_type=PartnerMarginAccrual.SourceType.SUBSCRIPTION,
        source_uuid=subscription.pk,
        billing_date=billing_date,
    )
    accrual = PartnerMarginAccrual.objects.get()
    assert accrual.partner_channel_id == channel.pk
    assert accrual.customer_user_id == user.pk
    assert accrual.customer_user_id_snapshot == user.pk
    assert accrual.source_type == PartnerMarginAccrual.SourceType.SUBSCRIPTION
    assert accrual.source_id == source_id
    assert accrual.source_id == f"{subscription.pk}:{billing_date.isoformat()}"
    assert accrual.list_price == Decimal("25.000000")
    assert accrual.net_price == Decimal("5.000000")
    assert accrual.margin == Decimal("20.000000")
    assert accrual.partner_share == Decimal(expected_share)
    assert accrual.revenue_share_percent == Decimal(percent)
    entry = accrual.ledger_entry
    assert entry.reference_type == LedgerReferenceType.PARTNER_MARGIN
    assert entry.reference_id == str(accrual.pk)
    assert entry.idempotency_key == f"partner-margin:subscription:{source_id}"
    assert entry.delta == Decimal(expected_share)
    assert entry.account_id == channel.organization.account_id
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.RENEWED
    assert cycle.billing_date == billing_date


@ENABLED
@pytest.mark.django_db
def test_half_up_on_sixth_decimal_boundary() -> None:
    """0.0000005 quantizes to 0.000001. Package columns cannot store this net."""
    user = _customer("boundary@example.com")
    _channel(user, percent="50.00")
    accrual = partner_margin_service.accrue(
        source_type=PartnerMarginAccrual.SourceType.SUBSCRIPTION,
        source_id=build_source_id(
            source_type=PartnerMarginAccrual.SourceType.SUBSCRIPTION,
            source_uuid=uuid.uuid4(),
            billing_date=timezone.localdate(),
        ),
        list_price=Decimal("1.000000"),
        net_price=Decimal("0.999999"),
        customer=user,
    )
    assert accrual is not None
    assert accrual.margin == Decimal("0.000001")
    assert accrual.partner_share == Decimal("0.000001")


@ENABLED
@pytest.mark.django_db
def test_partner_credit_failure_rolls_back_renewal(monkeypatch) -> None:
    user = _customer("rollback@example.com")
    _fund(user)
    subscription = _subscription(user, list_price="25.00", net_price="5.00")
    _channel(user, percent="50.00")
    billing_date = subscription.next_billing_date

    def _boom(*args, **kwargs):
        raise RuntimeError("partner credit failed")

    monkeypatch.setattr(partner_margin_service._credits, "credit", _boom)
    with pytest.raises(RuntimeError, match="partner credit failed"):
        _renew(subscription)

    subscription.refresh_from_db()
    user.billing_account.refresh_from_db()
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert subscription.status == Subscription.Status.ACTIVE
    assert subscription.next_billing_date == billing_date
    assert cycle.status == SubscriptionRenewalCycle.Status.PENDING
    assert user.billing_account.balance == Decimal("20.000000")
    assert PartnerMarginAccrual.objects.count() == 0
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION
        ).count()
        == 0
    )
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 0
    )
