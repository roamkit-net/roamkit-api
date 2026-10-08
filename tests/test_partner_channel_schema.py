"""Schema tests for Partner Channel (ADR 023). No services."""

from __future__ import annotations

import importlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.billing.models import (
    REFERENCE_MODELS,
    AppendOnlyViolation,
    CreditLedgerEntry,
    LedgerReferenceType,
    Subscription,
)
from apps.billing.partner_channel import (
    CustomerAttribution,
    CustomerAttributionHistory,
    PartnerChannel,
    PartnerCreditGrant,
    PartnerInviteLink,
    PartnerMarginAccrual,
    PendingPartnerAttribution,
    SubscriptionRenewalCycle,
)
from apps.catalog.models import Package
from apps.esims.models import Esim, Topup, TopupNetPriceImmutable
from apps.orders.models import Order
from apps.organizations.services.account_binding import create_organization

User = get_user_model()


def _reverse_partner_ledger_types() -> None:
    from django.apps import apps

    migration = importlib.import_module(
        "apps.billing.migrations.0006_partner_channel_schema"
    )
    migration.reverse_partner_ledger_reference_types(apps, None)


def _user(email: str) -> User:
    return User.objects.create_user(email=email, password="secret123")


def _channel(owner: User, *, percent: str = "50.00") -> PartnerChannel:
    org = create_organization(name=f"Org {owner.email}", actor=owner)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal(percent),
    )


def _link(channel: PartnerChannel, token: str) -> PartnerInviteLink:
    return PartnerInviteLink.objects.create(partner_channel=channel, token=token)


def _attribution(
    user: User,
    channel: PartnerChannel,
    *,
    source: str = CustomerAttribution.Source.ADMIN,
) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        source=source,
        attributed_at=timezone.now(),
    )


def _ledger(account, key: str, *, reference_type: str) -> CreditLedgerEntry:
    return CreditLedgerEntry.objects.create(
        account=account,
        delta=Decimal("1.000000"),
        balance_after=Decimal("1.000000"),
        reference_type=reference_type,
        reference_id=key,
        idempotency_key=key,
    )


def _esim(user: User) -> Esim:
    package = Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{uuid.uuid4()}",
        customer_ref=f"ref-{uuid.uuid4()}",
    )
    return Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid=f"89{uuid.uuid4().int % 10**18:018d}"[:20],
        status=Esim.Status.ACTIVATED,
    )


@pytest.mark.django_db
def test_partner_reference_models_point_at_audit_rows() -> None:
    assert REFERENCE_MODELS[LedgerReferenceType.PARTNER_MARGIN] is PartnerMarginAccrual
    assert REFERENCE_MODELS[LedgerReferenceType.PARTNER_GRANT_OUT] is PartnerCreditGrant
    assert REFERENCE_MODELS[LedgerReferenceType.PARTNER_GRANT_IN] is PartnerCreditGrant


@pytest.mark.django_db
def test_revenue_share_percent_check() -> None:
    owner = _user("share@example.com")
    org = create_organization(name="Share", actor=owner)
    PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("0"),
    )
    other = _user("share-high@example.com")
    org_high = create_organization(name="Share high", actor=other)
    PartnerChannel.objects.create(
        organization=org_high,
        revenue_share_percent=Decimal("100.00"),
    )
    low = _user("share-low@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            organization=create_organization(name="Too low", actor=low),
            revenue_share_percent=Decimal("-0.01"),
        )
    high = _user("share-over@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            organization=create_organization(name="Too high", actor=high),
            revenue_share_percent=Decimal("100.01"),
        )


@pytest.mark.django_db
def test_channel_and_link_are_unique_and_not_deleted() -> None:
    owner = _user("channel@example.com")
    channel = _channel(owner)
    link = _link(channel, "token-one")
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            organization=channel.organization,
            revenue_share_percent=Decimal("10"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerInviteLink.objects.create(partner_channel=channel, token="token-two")
    with pytest.raises(IntegrityError), transaction.atomic():
        other = _channel(_user("other-channel@example.com"))
        PartnerInviteLink.objects.create(partner_channel=other, token="token-one")
    with pytest.raises(AppendOnlyViolation):
        channel.delete()
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.filter(pk=channel.pk).delete()
    with pytest.raises(AppendOnlyViolation):
        link.delete()


@pytest.mark.django_db(transaction=True)
def test_organization_delete_is_blocked_by_partner_channel() -> None:
    owner = _user("protect@example.com")
    channel = _channel(owner)
    with pytest.raises(IntegrityError):
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM organizations_organization WHERE id = %s",
                [channel.organization_id],
            )
    assert PartnerChannel.objects.filter(pk=channel.pk).exists()


@pytest.mark.django_db
def test_customer_attribution_is_unique_per_user() -> None:
    owner = _user("attr-owner@example.com")
    channel = _channel(owner)
    customer = _user("customer@example.com")
    _attribution(customer, channel)
    other = _channel(_user("attr-other@example.com"))
    with pytest.raises(IntegrityError), transaction.atomic():
        _attribution(customer, other)


@pytest.mark.django_db
def test_history_is_append_only_and_indexed() -> None:
    owner = _user("hist-owner@example.com")
    source = _channel(owner)
    target = _channel(_user("hist-target@example.com"))
    customer = _user("hist-customer@example.com")
    row = CustomerAttributionHistory.objects.create(
        user=customer,
        from_partner_channel=source,
        to_partner_channel=target,
        changed_by=owner,
        changed_by_user_id_snapshot=owner.id,
        change_reason="ops transfer",
        changed_at=timezone.now(),
        previous_attributed_at=timezone.now() - timedelta(days=1),
    )
    row.change_reason = "edited"
    with pytest.raises(AppendOnlyViolation):
        row.save()
    with pytest.raises(AppendOnlyViolation):
        row.delete()
    with pytest.raises(AppendOnlyViolation):
        CustomerAttributionHistory.objects.filter(pk=row.pk).update(
            change_reason="edited"
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            ["bill_attr_history_user_at"],
        )
        assert cursor.fetchone() is not None


@pytest.mark.django_db
def test_pending_attribution_expires_index_and_user_unique() -> None:
    owner = _user("pending-owner@example.com")
    channel = _channel(owner)
    user = _user("pending@example.com")
    PendingPartnerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        invite_token_snapshot="tok",
        expires_at=timezone.now() + timedelta(hours=24),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PendingPartnerAttribution.objects.create(
            user=user,
            partner_channel=channel,
            invite_token_snapshot="tok-2",
            expires_at=timezone.now() + timedelta(hours=1),
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            ["bill_pending_attr_expires"],
        )
        assert cursor.fetchone() is not None


@pytest.mark.django_db
def test_accrual_uniques_and_immutability() -> None:
    owner = _user("accrual-owner@example.com")
    channel = _channel(owner)
    customer = _user("accrual-customer@example.com")
    attribution = _attribution(customer, channel)
    account = channel.organization.account
    ledger = _ledger(
        account,
        "partner-margin:order:1",
        reference_type=LedgerReferenceType.PARTNER_MARGIN,
    )
    accrual = PartnerMarginAccrual.objects.create(
        partner_channel=channel,
        customer_user=customer,
        customer_user_id_snapshot=customer.id,
        customer_attribution=attribution,
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=str(uuid.uuid4()),
        list_price=Decimal("25.000000"),
        net_price=Decimal("5.000000"),
        margin=Decimal("20.000000"),
        revenue_share_percent=Decimal("50.00"),
        partner_share=Decimal("10.000000"),
        ledger_entry=ledger,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        other_ledger = _ledger(
            account,
            "partner-margin:order:2",
            reference_type=LedgerReferenceType.PARTNER_MARGIN,
        )
        PartnerMarginAccrual.objects.create(
            partner_channel=channel,
            customer_user_id_snapshot=customer.id,
            source_type=accrual.source_type,
            source_id=accrual.source_id,
            list_price=Decimal("25.000000"),
            net_price=Decimal("5.000000"),
            margin=Decimal("20.000000"),
            revenue_share_percent=Decimal("50.00"),
            partner_share=Decimal("10.000000"),
            ledger_entry=other_ledger,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerMarginAccrual.objects.create(
            partner_channel=channel,
            customer_user_id_snapshot=customer.id,
            source_type=PartnerMarginAccrual.SourceType.TOPUP,
            source_id=str(uuid.uuid4()),
            list_price=Decimal("25.000000"),
            net_price=Decimal("5.000000"),
            margin=Decimal("20.000000"),
            revenue_share_percent=Decimal("50.00"),
            partner_share=Decimal("10.000000"),
            ledger_entry=ledger,
        )
    with pytest.raises(AppendOnlyViolation):
        accrual.partner_share = Decimal("1.000000")
        accrual.save()
    with pytest.raises(AppendOnlyViolation):
        PartnerMarginAccrual.objects.filter(pk=accrual.pk).delete()


@pytest.mark.django_db
def test_grant_amount_and_idempotency() -> None:
    owner = _user("grant-owner@example.com")
    channel = _channel(owner)
    customer = _user("grant-customer@example.com")
    attribution = _attribution(customer, channel)
    account = channel.organization.account
    debit = _ledger(
        account,
        "grant-out-1",
        reference_type=LedgerReferenceType.PARTNER_GRANT_OUT,
    )
    credit = _ledger(
        customer.billing_account,
        "grant-in-1",
        reference_type=LedgerReferenceType.PARTNER_GRANT_IN,
    )
    grant = PartnerCreditGrant.objects.create(
        partner_channel=channel,
        customer_user=customer,
        customer_user_id_snapshot=customer.id,
        customer_attribution=attribution,
        granted_by=owner,
        granted_by_user_id_snapshot=owner.id,
        amount=Decimal("10.000000"),
        idempotency_key="grant-key-1",
        debit_ledger_entry=debit,
        credit_ledger_entry=credit,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerCreditGrant.objects.create(
            partner_channel=channel,
            customer_user_id_snapshot=customer.id,
            customer_attribution=attribution,
            granted_by_user_id_snapshot=owner.id,
            amount=Decimal("1.000000"),
            idempotency_key=grant.idempotency_key,
            debit_ledger_entry=_ledger(
                account,
                "grant-out-2",
                reference_type=LedgerReferenceType.PARTNER_GRANT_OUT,
            ),
            credit_ledger_entry=_ledger(
                customer.billing_account,
                "grant-in-2",
                reference_type=LedgerReferenceType.PARTNER_GRANT_IN,
            ),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerCreditGrant.objects.create(
            partner_channel=channel,
            customer_user_id_snapshot=customer.id,
            customer_attribution=attribution,
            granted_by_user_id_snapshot=owner.id,
            amount=Decimal("0"),
            idempotency_key="grant-key-zero",
            debit_ledger_entry=_ledger(
                account,
                "grant-out-3",
                reference_type=LedgerReferenceType.PARTNER_GRANT_OUT,
            ),
            credit_ledger_entry=_ledger(
                customer.billing_account,
                "grant-in-3",
                reference_type=LedgerReferenceType.PARTNER_GRANT_IN,
            ),
        )
    with pytest.raises(AppendOnlyViolation):
        grant.save()


def _cycle(subscription: Subscription, billing_date: date, status: str):
    return SubscriptionRenewalCycle.objects.create(
        subscription=subscription,
        billing_date=billing_date,
        renewal_list_price_usd=Decimal("25.000000"),
        renewal_net_price_usd=Decimal("5.000000"),
        status=status,
    )


@pytest.mark.django_db
def test_renewal_cycle_partial_unique_and_write_once() -> None:
    user = _user("renewal@example.com")
    esim = _esim(user)
    subscription = Subscription.objects.create(
        account=user.billing_account,
        esim=esim,
        price_per_period=Decimal("25.000000"),
        next_billing_date=date(2026, 10, 8),
    )
    pending = _cycle(
        subscription,
        date(2026, 10, 8),
        SubscriptionRenewalCycle.Status.PENDING,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        _cycle(
            subscription,
            date(2026, 11, 7),
            SubscriptionRenewalCycle.Status.PAUSED,
        )
    pending.status = SubscriptionRenewalCycle.Status.RENEWED
    pending.save()
    later = _cycle(
        subscription,
        date(2026, 11, 7),
        SubscriptionRenewalCycle.Status.PENDING,
    )
    later.status = SubscriptionRenewalCycle.Status.RENEWED
    later.save()
    _cycle(
        subscription,
        date(2026, 12, 7),
        SubscriptionRenewalCycle.Status.PENDING,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        _cycle(
            subscription,
            date(2026, 10, 8),
            SubscriptionRenewalCycle.Status.RENEWED,
        )
    pending.renewal_list_price_usd = Decimal("1.000000")
    with pytest.raises(AppendOnlyViolation):
        pending.save()
    with pytest.raises(AppendOnlyViolation):
        pending.status = SubscriptionRenewalCycle.Status.PENDING
        pending.save()
    with pytest.raises(AppendOnlyViolation):
        SubscriptionRenewalCycle.objects.filter(pk=later.pk).update(
            status=SubscriptionRenewalCycle.Status.PENDING
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ["billing_renewal_one_unfinished"],
        )
        definition = cursor.fetchone()[0]
    assert "pending" in definition
    assert "paused" in definition
    assert "failed" in definition
    assert "renewed" not in definition


@pytest.mark.django_db
def test_topup_net_price_is_write_once() -> None:
    user = _user("topup-net@example.com")
    esim = _esim(user)
    topup = Topup.objects.create(
        account=user.billing_account,
        esim=esim,
        package_external_id="pkg",
        amount=Decimal("10.000000"),
        net_price_usd=None,
    )
    topup.status = Topup.Status.FULFILLED
    topup.save(update_fields=["status", "updated_at"])
    topup.refresh_from_db()
    assert topup.net_price_usd is None
    topup.net_price_usd = Decimal("4.000000")
    with pytest.raises(TopupNetPriceImmutable):
        topup.save()
    with pytest.raises(TopupNetPriceImmutable):
        Topup.objects.filter(pk=topup.pk).update(net_price_usd=Decimal("4.000000"))
    Topup.objects.filter(pk=topup.pk).update(status=Topup.Status.FAILED)
    topup.refresh_from_db()
    assert topup.status == Topup.Status.FAILED
    assert topup.net_price_usd is None


@pytest.mark.django_db
def test_reverse_stops_when_partner_ledger_rows_exist() -> None:
    user = _user("reverse@example.com")
    _ledger(
        user.billing_account,
        "partner-margin:keep",
        reference_type=LedgerReferenceType.PARTNER_MARGIN,
    )
    before = CreditLedgerEntry.objects.count()
    with pytest.raises(RuntimeError, match="partner_margin"):
        _reverse_partner_ledger_types()
    assert CreditLedgerEntry.objects.count() == before


@pytest.mark.django_db
def test_reverse_allows_empty_partner_ledger() -> None:
    user = _user("reverse-empty@example.com")
    _ledger(
        user.billing_account,
        "deposit-keep",
        reference_type=LedgerReferenceType.DEPOSIT,
    )
    before = CreditLedgerEntry.objects.count()
    _reverse_partner_ledger_types()
    assert CreditLedgerEntry.objects.count() == before
