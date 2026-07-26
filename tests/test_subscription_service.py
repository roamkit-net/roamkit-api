"""Tests for subscription renewal (PR8)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.exceptions import SubscriptionsDisabledError
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType, Subscription
from apps.billing.services import credit_service, subscription_service
from apps.billing.services.subscription import SUBSCRIPTION_PERIOD_DAYS
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
    paused: list[SubscriptionPaused] = []
    event_bus.subscribe(SubscriptionPaused, paused.append)
    try:
        result = subscription_service.renew_one(subscription.pk)
    finally:
        event_bus._handlers[SubscriptionPaused].remove(paused.append)

    subscription.refresh_from_db()
    assert result == "paused"
    assert subscription.status == Subscription.Status.PAUSED
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
