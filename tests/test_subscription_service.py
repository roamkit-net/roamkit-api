"""Tests for subscription renewal (PR8)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.exceptions import SubscriptionsDisabledError
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType, Subscription
from apps.billing.partner_channel import SubscriptionRenewalCycle
from apps.billing.services import credit_service, subscription_service
from apps.billing.services.subscription import (
    SUBSCRIPTION_PERIOD_DAYS,
    get_or_create_renewal_cycle,
)
from apps.billing.tasks import renew_subscriptions
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from shared.events.billing_events import (
    CreditDebited,
    SubscriptionPaused,
    SubscriptionRenewed,
)
from shared.events.event_bus import event_bus


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="sub@example.com", password="secret123")


@pytest.fixture
def account(user: User):
    return user.billing_account


@pytest.fixture
def esim(user: User, account) -> Esim:
    package = Package.objects.create(
        external_id="pkg-sub-1",
        title="Sub Pack",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=30,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    return Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid="891000000000009999",
        status=Esim.Status.ACTIVATED,
    )


@pytest.fixture
def subscription(account, esim) -> Subscription:
    return Subscription.objects.create(
        account=account,
        esim=esim,
        price_per_period=Decimal("5.000000"),
        next_billing_date=timezone.localdate(),
        status=Subscription.Status.ACTIVE,
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renew_debits_and_advances_date(account, subscription) -> None:
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-sub-1",
        idempotency_key="dep-sub-1",
    )
    renewed: list[SubscriptionRenewed] = []
    debited: list[CreditDebited] = []
    event_bus.subscribe(SubscriptionRenewed, renewed.append)
    event_bus.subscribe(CreditDebited, debited.append)

    try:
        result = subscription_service.renew_one(subscription.pk)
    finally:
        event_bus._handlers[SubscriptionRenewed].remove(renewed.append)
        event_bus._handlers[CreditDebited].remove(debited.append)

    subscription.refresh_from_db()
    account.refresh_from_db()

    assert result == "renewed"
    assert subscription.status == Subscription.Status.ACTIVE
    assert subscription.next_billing_date == timezone.localdate() + timedelta(
        days=SUBSCRIPTION_PERIOD_DAYS
    )
    assert account.balance == Decimal("15.000000")
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION,
            reference_id=str(subscription.pk),
        ).count()
        == 1
    )
    assert len(renewed) == 1
    assert len(debited) == 1
    assert renewed[0].amount == Decimal("5.000000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renew_pauses_when_underfunded(account, subscription) -> None:
    billing_date = subscription.next_billing_date
    paused: list[SubscriptionPaused] = []
    event_bus.subscribe(SubscriptionPaused, paused.append)
    try:
        result = subscription_service.renew_one(subscription.pk)
    finally:
        event_bus._handlers[SubscriptionPaused].remove(paused.append)

    subscription.refresh_from_db()
    assert result == "paused"
    assert subscription.status == Subscription.Status.PAUSED
    assert subscription.next_billing_date == billing_date
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.PAUSED
    assert cycle.billing_date == billing_date
    assert len(paused) == 1
    assert paused[0].deposit_url.endswith("/me/deposit")
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION
        ).count()
        == 0
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renew_idempotent_same_billing_date(account, subscription) -> None:
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-sub-2",
        idempotency_key="dep-sub-2",
    )
    assert subscription_service.renew_one(subscription.pk) == "renewed"
    # Next billing date is in the future → skipped
    assert subscription_service.renew_one(subscription.pk) == "skipped"
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renew_due_processes_batch(account, esim) -> None:
    credit_service.credit(
        account,
        Decimal("50.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-batch",
        idempotency_key="dep-batch",
    )
    today = timezone.localdate()
    Subscription.objects.create(
        account=account,
        esim=esim,
        price_per_period=Decimal("3.000000"),
        next_billing_date=today - timedelta(days=1),
        status=Subscription.Status.ACTIVE,
    )
    Subscription.objects.create(
        account=account,
        esim=esim,
        price_per_period=Decimal("3.000000"),
        next_billing_date=today + timedelta(days=5),
        status=Subscription.Status.ACTIVE,
    )
    stats = subscription_service.renew_due(as_of=today)
    assert stats["renewed"] == 1
    assert stats["paused"] == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=False)
def test_renew_respects_subscriptions_flag(subscription) -> None:
    with pytest.raises(SubscriptionsDisabledError):
        subscription_service.renew_one(subscription.pk)


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=False)
def test_celery_task_skips_when_disabled() -> None:
    result = renew_subscriptions()
    assert result.get("disabled") == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renew_commits_cycle_before_debit(account, subscription) -> None:
    package = subscription.esim.order.package
    package.net_price_usd = Decimal("4.00")
    package.save(update_fields=["net_price_usd", "updated_at"])
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-cycle-1",
        idempotency_key="dep-cycle-1",
    )

    assert subscription_service.renew_one(subscription.pk) == "renewed"

    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.RENEWED
    assert cycle.billing_date == timezone.localdate()
    subscription.refresh_from_db()
    assert subscription.next_billing_date == cycle.billing_date + timedelta(
        days=SUBSCRIPTION_PERIOD_DAYS
    )
    assert cycle.renewal_list_price_usd == Decimal("10.000000")
    assert cycle.renewal_net_price_usd == Decimal("4.000000")
    assert cycle.renewal_list_price_usd != subscription.price_per_period


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_cycle_survives_debit_failure_and_retry_keeps_prices(
    account, subscription, monkeypatch
) -> None:
    package = subscription.esim.order.package
    package.price_usd = Decimal("10.00")
    package.net_price_usd = Decimal("4.00")
    package.save(update_fields=["price_usd", "net_price_usd", "updated_at"])

    def _boom(*args, **kwargs):
        raise RuntimeError("debit failed")

    monkeypatch.setattr(subscription_service._credits, "debit", _boom)
    with pytest.raises(RuntimeError, match="debit failed"):
        subscription_service.renew_one(subscription.pk)

    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.PENDING
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION
        ).count()
        == 0
    )
    subscription.refresh_from_db()
    assert subscription.next_billing_date == cycle.billing_date

    package.price_usd = Decimal("99.00")
    package.net_price_usd = Decimal("1.00")
    package.save(update_fields=["price_usd", "net_price_usd", "updated_at"])
    with pytest.raises(RuntimeError, match="debit failed"):
        subscription_service.renew_one(subscription.pk)

    cycle.refresh_from_db()
    assert (
        SubscriptionRenewalCycle.objects.filter(subscription=subscription).count() == 1
    )
    assert cycle.renewal_list_price_usd == Decimal("10.000000")
    assert cycle.renewal_net_price_usd == Decimal("4.000000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_second_unfinished_cycle_is_rejected(subscription) -> None:
    first = get_or_create_renewal_cycle(subscription, subscription.next_billing_date)
    assert first.status == SubscriptionRenewalCycle.Status.PENDING
    with pytest.raises(IntegrityError):
        get_or_create_renewal_cycle(
            subscription,
            subscription.next_billing_date + timedelta(days=30),
        )
    assert (
        SubscriptionRenewalCycle.objects.filter(subscription=subscription).count() == 1
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_success_renews_cycle_from_billing_date_not_today(
    account, subscription
) -> None:
    billing_date = timezone.localdate() - timedelta(days=7)
    subscription.next_billing_date = billing_date
    subscription.save(update_fields=["next_billing_date", "updated_at"])
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-cycle-late",
        idempotency_key="dep-cycle-late",
    )

    assert subscription_service.renew_one(subscription.pk) == "renewed"

    subscription.refresh_from_db()
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    assert cycle.status == SubscriptionRenewalCycle.Status.RENEWED
    assert cycle.billing_date == billing_date
    assert subscription.next_billing_date == billing_date + timedelta(
        days=SUBSCRIPTION_PERIOD_DAYS
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, SUBSCRIPTIONS_ENABLED=True)
def test_renewed_cycle_does_not_debit_again(account, subscription) -> None:
    credit_service.credit(
        account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id="dep-cycle-once",
        idempotency_key="dep-cycle-once",
    )
    assert subscription_service.renew_one(subscription.pk) == "renewed"
    cycle = SubscriptionRenewalCycle.objects.get(subscription=subscription)
    subscription.next_billing_date = cycle.billing_date
    subscription.save(update_fields=["next_billing_date", "updated_at"])

    assert subscription_service.renew_one(subscription.pk) == "renewed"

    account.refresh_from_db()
    assert account.balance == Decimal("15.000000")
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.SUBSCRIPTION,
            reference_id=str(subscription.pk),
        ).count()
        == 1
    )
    cycle.refresh_from_db()
    assert cycle.status == SubscriptionRenewalCycle.Status.RENEWED
    subscription.refresh_from_db()
    assert subscription.next_billing_date == cycle.billing_date
