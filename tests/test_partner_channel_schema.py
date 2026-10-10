"""Schema tests for Partner Channel (ADR 023)."""

from __future__ import annotations

import importlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.test import Client, override_settings
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
    InviteVisit,
    PartnerChannel,
    PartnerCreditGrant,
    PartnerInviteLink,
    PartnerMarginAccrual,
    PendingPartnerAttribution,
    SubscriptionRenewalCycle,
)
from apps.billing.services.partner_attribution import invite_snapshot_from_visit
from apps.billing.services.partner_invite import (
    PartnerInviteError,
    canonical_invite_link,
    create_partner_channel,
    invite_link_for,
    regenerate_invite_link,
    set_invite_active,
)
from apps.billing.services.partner_invite_visit import record_visit
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
    assert (
        REFERENCE_MODELS[LedgerReferenceType.PARTNER_INVITE_BONUS]
        is CustomerAttribution
    )


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
    second = PartnerInviteLink.objects.create(
        partner_channel=channel, token="token-two"
    )
    assert second.partner_channel_id == channel.pk
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
def test_attribution_snapshot_columns_and_shared_visit() -> None:
    owner = _user("snap-owner@example.com")
    channel = _channel(owner)
    link = _link(channel, "snap-token")
    link.name = "October"
    link.source = "tiktok"
    link.campaign = "fall"
    link.content = "video"
    link.save(update_fields=["name", "source", "campaign", "content", "updated_at"])
    visit = InviteVisit.objects.create(
        invite_link=link,
        utm_source="tiktok",
        utm_medium="social",
        utm_campaign="fall-utm",
        utm_content="bio",
    )
    first = _user("snap-a@example.com")
    second = _user("snap-b@example.com")
    _attribution(first, channel)
    CustomerAttribution.objects.create(
        user=second,
        partner_channel=channel,
        source=CustomerAttribution.Source.INVITE_LINK,
        invite_visit=visit,
        attributed_at=timezone.now(),
    )
    legacy = CustomerAttribution.objects.get(user=first)
    shared = CustomerAttribution.objects.get(user=second)
    assert CustomerAttribution._meta.get_field("user").one_to_one
    visit_field = CustomerAttribution._meta.get_field("invite_visit")
    assert visit_field.null and not visit_field.unique
    assert visit_field.remote_field.on_delete is models.PROTECT
    assert legacy.invite_visit_id is None
    assert legacy.registered_via_invite is False
    assert legacy.bonus_amount_snapshot is None
    assert shared.invite_visit_id == visit.pk
    assert shared.registered_via_invite is False
    lengths = {
        "invite_name_snapshot": 128,
        "invite_source_snapshot": 64,
        "invite_campaign_snapshot": 64,
        "invite_content_snapshot": 64,
        "utm_source_snapshot": 128,
        "utm_medium_snapshot": 128,
        "utm_campaign_snapshot": 128,
        "utm_content_snapshot": 128,
    }
    for name, length in lengths.items():
        assert CustomerAttribution._meta.get_field(name).max_length == length
    bonus = CustomerAttribution._meta.get_field("bonus_amount_snapshot")
    assert bonus.null and bonus.default is models.NOT_PROVIDED

    legacy.partner_channel = channel
    legacy.save(update_fields=["partner_channel"])
    legacy.invite_token = "changed"
    with pytest.raises(AppendOnlyViolation):
        legacy.save(update_fields=["invite_token"])
    with pytest.raises(AppendOnlyViolation):
        CustomerAttribution.objects.filter(pk=legacy.pk).update(
            bonus_amount_snapshot=Decimal("1.000000")
        )


@pytest.mark.django_db
def test_pending_visit_is_optional_and_protected() -> None:
    owner = _user("pend-visit-owner@example.com")
    channel = _channel(owner)
    link = _link(channel, "pend-token")
    visit = InviteVisit.objects.create(invite_link=link)
    legacy_user = _user("pend-legacy@example.com")
    linked_user = _user("pend-linked@example.com")
    PendingPartnerAttribution.objects.create(
        user=legacy_user,
        partner_channel=channel,
        invite_token_snapshot="pend-token",
        expires_at=timezone.now() + timedelta(hours=24),
    )
    pending = PendingPartnerAttribution.objects.create(
        user=linked_user,
        partner_channel=channel,
        invite_token_snapshot="pend-token",
        invite_visit=visit,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    field = PendingPartnerAttribution._meta.get_field("invite_visit")
    assert field.null and field.remote_field.on_delete is models.PROTECT
    assert pending.invite_visit_id == visit.pk
    assert (
        PendingPartnerAttribution.objects.get(user=legacy_user).invite_visit_id is None
    )


@pytest.mark.django_db
def test_invite_snapshot_copies_link_and_visit_at_call_time() -> None:
    owner = _user("helper-owner@example.com")
    channel = _channel(owner)
    link = _link(channel, "helper-token")
    link.name = "October"
    link.source = "tiktok"
    link.campaign = "fall"
    link.content = "video"
    link.save(update_fields=["name", "source", "campaign", "content", "updated_at"])
    visit = InviteVisit.objects.create(
        invite_link=link,
        utm_source="TikTok",
        utm_medium="cpc",
        utm_campaign="Fall",
        utm_content="bio",
    )
    snapshot = invite_snapshot_from_visit(visit)
    link.name = "November"
    link.save(update_fields=["name", "updated_at"])
    assert snapshot == {
        "invite_visit": visit,
        "invite_token": "helper-token",
        "invite_name_snapshot": "October",
        "invite_source_snapshot": "tiktok",
        "invite_campaign_snapshot": "fall",
        "invite_content_snapshot": "video",
        "utm_source_snapshot": "TikTok",
        "utm_medium_snapshot": "cpc",
        "utm_campaign_snapshot": "Fall",
        "utm_content_snapshot": "bio",
    }
    assert "registered_via_invite" not in snapshot
    assert "bonus_amount_snapshot" not in snapshot


@pytest.mark.django_db
def test_admin_attribution_stays_without_visit_or_bonus() -> None:
    owner = _user("admin-attr-owner@example.com")
    channel = _channel(owner)
    customer = _user("admin-attr@example.com")
    row = _attribution(customer, channel, source=CustomerAttribution.Source.ADMIN)
    assert row.invite_visit_id is None
    assert row.registered_via_invite is False
    assert row.bonus_amount_snapshot is None
    assert row.invite_name_snapshot == ""
    assert row.utm_source_snapshot == ""
    assert InviteVisit.objects.count() == 0


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


@pytest.mark.django_db
def test_negative_invite_bonus_is_rejected() -> None:
    owner = _user("bonus-neg@example.com")
    channel = _channel(owner)
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerInviteLink.objects.create(
            partner_channel=channel,
            token="neg-bonus",
            bonus_amount=Decimal("-0.000001"),
        )


@pytest.mark.django_db
def test_canonical_link_is_oldest_created_at_then_id() -> None:
    owner = _user("canonical@example.com")
    channel = _channel(owner)
    first = _link(channel, "canon-a")
    second = _link(channel, "canon-b")
    stamp = timezone.now() - timedelta(days=3)
    PartnerInviteLink.objects.filter(pk__in=[first.pk, second.pk]).update(
        created_at=stamp
    )
    expected = min((first, second), key=lambda row: (stamp, row.id))
    assert canonical_invite_link(channel).pk == expected.pk

    later = timezone.now()
    PartnerInviteLink.objects.filter(pk=first.pk).update(
        created_at=later - timedelta(seconds=1)
    )
    PartnerInviteLink.objects.filter(pk=second.pk).update(created_at=later)
    assert canonical_invite_link(channel).pk == first.pk


@pytest.mark.django_db
@override_settings(PARTNER_JOIN_BASE_URL="https://roamkit.net")
def test_campaign_link_does_not_replace_canonical() -> None:
    owner = _user("campaign-add@example.com")
    channel = _channel(owner)
    portal = _link(channel, "portal-token")
    PartnerInviteLink.objects.filter(pk=portal.pk).update(
        created_at=timezone.now() - timedelta(days=1)
    )
    _link(channel, "campaign-token")
    assert canonical_invite_link(channel).pk == portal.pk
    assert invite_link_for(channel).url.endswith("/join/portal-token")


@pytest.mark.django_db
def test_create_partner_channel_mints_zero_bonus_canonical_link() -> None:
    owner = _user("create-channel@example.com")
    org = create_organization(name="Mint org", actor=owner)
    channel = create_partner_channel(organization=org)
    link = canonical_invite_link(channel)
    assert channel.invite_links.count() == 1
    assert link.name == ""
    assert link.bonus_amount == Decimal("0.000000")
    assert link.source == ""
    assert link.campaign == ""
    assert link.content == ""
    assert link.token
    assert PartnerInviteLink._meta.get_field("created_at").auto_now_add is True
    assert PartnerInviteLink._meta.get_field("token").editable is False


@pytest.mark.django_db
def test_blank_invite_token_cannot_be_stored() -> None:
    owner = _user("blank-token@example.com")
    channel = _channel(owner)
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.create(partner_channel=channel, is_active=True)
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.create(partner_channel=channel, token=None)
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.create(partner_channel=channel, token="   ")
    link = _link(channel, "kept-token")
    link.name = "unchanged-name"
    link.save(update_fields=["name"])
    link.refresh_from_db()
    assert link.token == "kept-token"
    for blank in ("", None, "   "):
        with pytest.raises(ValidationError, match="requires a token"):
            PartnerInviteLink.objects.filter(pk=link.pk).update(token=blank)
    link.refresh_from_db()
    assert link.token == "kept-token"
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.get_or_create(
            partner_channel=channel,
            token="",
            defaults={"is_active": True},
        )
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.update_or_create(
            pk=link.pk,
            defaults={"token": ""},
        )
    link.token = ""
    with pytest.raises(ValidationError, match="requires a token"):
        PartnerInviteLink.objects.bulk_update([link], ["token"])
    link.refresh_from_db()
    assert link.token == "kept-token"
    assert PartnerInviteLink.objects.filter(partner_channel=channel).count() == 1


@pytest.mark.django_db
def test_existing_blank_token_cannot_be_saved_until_replaced() -> None:
    owner = _user("corrupt-token@example.com")
    channel = _channel(owner)
    link = PartnerInviteLink.objects.bulk_create(
        [
            PartnerInviteLink(
                partner_channel=channel,
                token="",
                is_active=True,
            )
        ]
    )[0]
    link.name = "renamed"
    with pytest.raises(ValidationError, match="requires a token"):
        link.save(update_fields=["name"])
    link.refresh_from_db()
    assert link.name == ""
    assert link.token == ""
    assert link.is_active is True
    link.is_active = False
    with pytest.raises(ValidationError, match="requires a token"):
        link.save(update_fields=["is_active"])
    link.refresh_from_db()
    assert link.is_active is True
    assert link.token == ""
    link.token = "repaired-token"
    link.is_active = False
    link.save(update_fields=["token", "is_active"])
    link.refresh_from_db()
    assert link.token == "repaired-token"
    assert link.is_active is False


@pytest.mark.django_db
def test_admin_partner_channel_add_page_opens() -> None:
    owner = _user("admin-channel-add@example.com")
    org = create_organization(name="North fleet", actor=owner)
    client = _staff_client("staff-channel-add@example.com")

    opened = client.get("/admin/billing/partnerchannel/add/")
    assert opened.status_code == 200
    assert b"Settlement account" not in opened.content

    created = client.post(
        "/admin/billing/partnerchannel/add/",
        {
            "kind": "team",
            "owner_user": "",
            "organization": str(org.pk),
            "is_active": "on",
            "revenue_share_percent": "40.00",
        },
    )
    assert created.status_code == 302
    channel = PartnerChannel.objects.get(organization=org)
    assert channel.kind == PartnerChannel.Kind.TEAM
    assert channel.owner_user_id is None
    assert channel.revenue_share_percent == Decimal("40.00")
    assert channel.is_active is True

    changed = client.get(f"/admin/billing/partnerchannel/{channel.pk}/change/")
    assert changed.status_code == 200
    assert b"Settlement account" in changed.content


@pytest.mark.django_db
def test_admin_add_mints_a_token_for_a_second_active_link() -> None:
    owner = _user("admin-invite@example.com")
    channel = _channel(owner)
    portal = _link(channel, "portal-token-value")
    staff = _user("staff-invite@example.com")
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    client = Client()
    client.force_login(staff)
    response = client.post(
        "/admin/billing/partnerinvitelink/add/",
        {
            "partner_channel": str(channel.pk),
            "name": "October bio",
            "bonus_amount": "10.000000",
            "source": "test",
            "campaign": "october",
            "content": "bio",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    rows = list(
        PartnerInviteLink.objects.filter(partner_channel=channel).order_by(
            "created_at", "id"
        )
    )
    assert len(rows) == 2
    assert rows[0].pk == portal.pk
    assert rows[0].token == "portal-token-value"
    added = rows[1]
    assert added.is_active is True
    assert added.token
    assert added.token != portal.token
    assert added.source == "test"
    assert added.campaign == "october"
    assert added.content == "bio"
    assert canonical_invite_link(channel).pk == portal.pk
    second_token = added.token
    regenerate_invite_link(channel, actor=owner)
    portal.refresh_from_db()
    added.refresh_from_db()
    assert portal.token != "portal-token-value"
    assert portal.token
    assert added.token == second_token
    assert added.is_active is True
    assert canonical_invite_link(channel).pk == portal.pk
    assert invite_link_for(channel).url.endswith(f"/join/{portal.token}")


def _admin_invite_form(channel: PartnerChannel, **overrides: str) -> dict[str, str]:
    payload = {
        "partner_channel": str(channel.pk),
        "name": "October bio",
        "bonus_amount": "10.000000",
        "source": "test",
        "campaign": "october",
        "content": "bio",
        "is_active": "on",
    }
    payload.update(overrides)
    return payload


def _staff_client(email: str) -> Client:
    staff = _user(email)
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    client = Client()
    client.force_login(staff)
    return client


@pytest.mark.django_db
def test_admin_change_does_not_rotate_invite_token() -> None:
    owner = _user("admin-change@example.com")
    channel = _channel(owner)
    link = _link(channel, "change-token-value")
    client = _staff_client("staff-change@example.com")
    response = client.post(
        f"/admin/billing/partnerinvitelink/{link.pk}/change/",
        _admin_invite_form(
            channel,
            name="Renamed",
            bonus_amount="2.000000",
            source="mail",
            campaign="spring",
            content="footer",
            is_active="",
        ),
    )
    assert response.status_code == 302
    link.refresh_from_db()
    assert link.token == "change-token-value"
    assert link.name == "Renamed"
    assert link.bonus_amount == Decimal("2.000000")
    assert link.source == "mail"
    assert link.campaign == "spring"
    assert link.content == "footer"
    assert link.is_active is False


@pytest.mark.django_db
def test_admin_add_retries_a_token_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _user("admin-collide@example.com")
    channel = _channel(owner)
    _link(channel, "taken-token")
    tokens = iter(["taken-token", "taken-token", "fresh-admin-token"])
    monkeypatch.setattr(
        "apps.billing.services.partner_invite.new_partner_invite_token",
        lambda: next(tokens),
    )
    client = _staff_client("staff-collide@example.com")
    response = client.post(
        "/admin/billing/partnerinvitelink/add/",
        _admin_invite_form(channel),
    )
    assert response.status_code == 302
    added = PartnerInviteLink.objects.exclude(token="taken-token").get(
        partner_channel=channel
    )
    assert added.token == "fresh-admin-token"
    assert (
        PartnerInviteLink.objects.filter(partner_channel=channel, token="").count() == 0
    )


@pytest.mark.django_db
def test_admin_add_collision_exhaustion_leaves_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("admin-exhaust@example.com")
    channel = _channel(owner)
    _link(channel, "taken-token")
    monkeypatch.setattr(
        "apps.billing.services.partner_invite.new_partner_invite_token",
        lambda: "taken-token",
    )
    client = _staff_client("staff-exhaust@example.com")
    before = set(PartnerInviteLink.objects.values_list("pk", flat=True))
    with pytest.raises(PartnerInviteError, match="unique invite token"):
        client.post(
            "/admin/billing/partnerinvitelink/add/",
            _admin_invite_form(channel),
        )
    assert set(PartnerInviteLink.objects.values_list("pk", flat=True)) == before


@pytest.mark.django_db
def test_regenerate_retries_a_token_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _user("regen-collide@example.com")
    other = _user("regen-other@example.com")
    org = create_organization(name="Regen collide", actor=owner)
    channel = create_partner_channel(organization=org)
    _link(_channel(other), "taken-token")
    tokens = iter(["taken-token", "regenerated-token"])
    monkeypatch.setattr(
        "apps.billing.services.partner_invite.new_partner_invite_token",
        lambda: next(tokens),
    )
    regenerate_invite_link(channel, actor=owner)
    link = canonical_invite_link(channel)
    assert channel.invite_links.count() == 1
    assert link.token == "regenerated-token"


@pytest.mark.django_db
@override_settings(PARTNER_CHANNEL_ENABLED=True)
def test_record_visit_ignores_blank_token() -> None:
    owner = _user("visit-blank@example.com")
    channel = _channel(owner)
    link = _link(channel, "visit-token")
    assert record_visit("") is None
    assert record_visit("   ") is None
    assert record_visit(None) is None
    assert InviteVisit.objects.filter(invite_link=link).count() == 0
    assert record_visit(link.token) is not None
    assert InviteVisit.objects.filter(invite_link=link).count() == 1


@pytest.mark.django_db
def test_service_lifecycle_keeps_one_nonempty_canonical_token() -> None:
    owner = _user("lifecycle-invite@example.com")
    org = create_organization(name="Lifecycle org", actor=owner)
    channel = create_partner_channel(organization=org)
    first = canonical_invite_link(channel).token
    assert first
    regenerated = regenerate_invite_link(channel, actor=owner)
    assert regenerated.url
    assert canonical_invite_link(channel).token not in ("", first)
    set_invite_active(channel, actor=owner, active=False)
    set_invite_active(channel, actor=owner, active=True)
    set_invite_active(channel, actor=owner, active=True)
    again = regenerate_invite_link(channel, actor=owner)
    link = canonical_invite_link(channel)
    assert channel.invite_links.count() == 1
    assert link.is_active is True
    assert link.token
    assert again.url.endswith(link.token)


@pytest.mark.django_db
@override_settings(PARTNER_JOIN_BASE_URL="https://roamkit.net")
def test_portal_mutations_follow_canonical_link() -> None:
    owner = _user("mutate-canon@example.com")
    channel = _channel(owner)
    portal = _link(channel, "portal-live")
    campaign = _link(channel, "campaign-live")
    PartnerInviteLink.objects.filter(pk=portal.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    PartnerInviteLink.objects.filter(pk=campaign.pk).update(
        created_at=timezone.now() - timedelta(days=1)
    )
    set_invite_active(channel, actor=owner, active=False)
    portal.refresh_from_db()
    campaign.refresh_from_db()
    assert portal.is_active is False
    assert campaign.is_active is True

    old_token = portal.token
    regenerate_invite_link(channel, actor=owner)
    portal.refresh_from_db()
    campaign.refresh_from_db()
    assert portal.token != old_token
    assert campaign.token == "campaign-live"
    assert invite_link_for(channel).url.endswith(f"/join/{portal.token}")


def _individual(owner: User) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("25.00"),
    )


@pytest.mark.django_db
def test_individual_and_team_owner_shapes() -> None:
    owner = _user("shape-owner@example.com")
    individual = _individual(owner)
    assert individual.kind == PartnerChannel.Kind.INDIVIDUAL
    assert individual.owner_user_id == owner.pk
    assert individual.organization_id is None

    team = _channel(_user("shape-team@example.com"))
    assert team.kind == PartnerChannel.Kind.TEAM
    assert team.owner_user_id is None
    assert team.organization_id is not None

    other = _user("shape-other@example.com")
    org = create_organization(name="Both owners", actor=other)
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            kind=PartnerChannel.Kind.INDIVIDUAL,
            owner_user=owner,
            organization=org,
            revenue_share_percent=Decimal("10"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            kind=PartnerChannel.Kind.INDIVIDUAL,
            owner_user=None,
            organization=None,
            revenue_share_percent=Decimal("10"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            kind=PartnerChannel.Kind.TEAM,
            owner_user=None,
            organization=None,
            revenue_share_percent=Decimal("10"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            kind=PartnerChannel.Kind.TEAM,
            owner_user=other,
            organization=None,
            revenue_share_percent=Decimal("10"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        _individual(owner)
    with pytest.raises(IntegrityError), transaction.atomic():
        PartnerChannel.objects.create(
            organization=team.organization,
            revenue_share_percent=Decimal("10"),
        )


@pytest.mark.django_db
def test_ownership_is_immutable_on_save_update_and_bulk_update() -> None:
    owner = _user("immutable@example.com")
    channel = _channel(owner)
    other = _user("immutable-other@example.com")

    channel.is_active = False
    channel.save(update_fields=["is_active"])
    PartnerChannel.objects.filter(pk=channel.pk).update(
        revenue_share_percent=Decimal("40.00")
    )
    row = PartnerChannel.objects.get(pk=channel.pk)
    row.revenue_share_percent = Decimal("41.00")
    PartnerChannel.objects.bulk_update([row], ["revenue_share_percent"])
    channel.refresh_from_db()
    assert channel.is_active is False
    assert channel.revenue_share_percent == Decimal("41.00")
    assert channel.kind == PartnerChannel.Kind.TEAM

    channel.kind = PartnerChannel.Kind.INDIVIDUAL
    with pytest.raises(AppendOnlyViolation):
        channel.save()
    with pytest.raises(AppendOnlyViolation):
        channel.save(update_fields=["kind"])
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.filter(pk=channel.pk).update(
            kind=PartnerChannel.Kind.INDIVIDUAL
        )
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.filter(pk=channel.pk).update(owner_user_id=other.pk)
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.filter(pk=channel.pk).update(organization_id=None)

    stolen = PartnerChannel.objects.get(pk=channel.pk)
    stolen.kind = PartnerChannel.Kind.INDIVIDUAL
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.bulk_update([stolen], ["kind"])
    stolen = PartnerChannel.objects.get(pk=channel.pk)
    stolen.owner_user = other
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.bulk_update([stolen], ["owner_user"])
    stolen = PartnerChannel.objects.get(pk=channel.pk)
    stolen.organization = None
    with pytest.raises(AppendOnlyViolation):
        PartnerChannel.objects.bulk_update([stolen], ["organization"])

    channel.refresh_from_db()
    assert channel.kind == PartnerChannel.Kind.TEAM
    assert channel.owner_user_id is None
    assert channel.organization_id is not None


@pytest.mark.django_db(transaction=True)
def test_individual_owner_delete_is_blocked_by_protect() -> None:
    owner = _user("individual-protect@example.com")
    channel = _individual(owner)
    with pytest.raises(IntegrityError):
        with connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {User._meta.db_table} WHERE id = %s",
                [owner.pk],
            )
    assert PartnerChannel.objects.filter(pk=channel.pk).exists()
    assert User.objects.filter(pk=owner.pk).exists()
