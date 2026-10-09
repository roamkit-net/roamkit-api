"""PartnerSummaryService (ADR 023). Read-only. No HTTP."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.billing.models import Account, LedgerReferenceType
from apps.billing.partner_channel import PartnerChannel, PartnerMarginAccrual
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_summary import partner_summary_service
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)


def _user(prefix: str) -> User:
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


def _accrual(
    channel: PartnerChannel,
    *,
    source_type: str,
    partner_share: str,
    snapshot_user_id: int = 1,
) -> None:
    entry = credit_service.credit(
        channel.organization.account,
        Decimal("0.500000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"sum-{uuid.uuid4()}",
        idempotency_key=f"sum-{uuid.uuid4()}",
    )
    PartnerMarginAccrual.objects.create(
        partner_channel=channel,
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


@ENABLED
@pytest.mark.django_db
def test_summary_sums_stored_shares_for_this_channel_only() -> None:
    owner = _user("owner")
    other_owner = _user("other")
    channel = _channel_for(owner)
    other = _channel_for(other_owner)
    _accrual(channel, source_type="order", partner_share="6.666000")
    _accrual(channel, source_type="order", partner_share="1.000000")
    _accrual(channel, source_type="topup", partner_share="2.500000")
    _accrual(channel, source_type="subscription", partner_share="0.000001")
    _accrual(other, source_type="order", partner_share="9.000000")
    channel.revenue_share_percent = Decimal("90.00")
    channel.save(update_fields=["revenue_share_percent", "updated_at"])

    summary = partner_summary_service.summarize(channel)

    assert summary.total_earned == Decimal("10.166001")
    assert summary.accrual_counts.order == 2
    assert summary.accrual_counts.topup == 1
    assert summary.accrual_counts.subscription == 1
    assert summary.accrual_counts.total == 4
    account = channel.organization.account
    account.refresh_from_db()
    assert summary.available_balance == account.balance
    assert summary.available_balance != summary.total_earned


@ENABLED
@pytest.mark.django_db
def test_empty_channel_is_zero_without_writing() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    before = Account.objects.count()

    summary = partner_summary_service.summarize(channel)

    assert summary.total_earned == Decimal("0.000000")
    assert summary.available_balance == Decimal("0.000000")
    assert summary.accrual_counts.order == 0
    assert summary.accrual_counts.topup == 0
    assert summary.accrual_counts.subscription == 0
    assert summary.accrual_counts.total == 0
    assert Account.objects.count() == before
    assert PartnerMarginAccrual.objects.count() == 0


@ENABLED
@pytest.mark.django_db
def test_negative_balance_is_clamped_only_on_the_dto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The balance check constraint still forbids a stored negative cache."""
    owner = _user("owner")
    channel = _channel_for(owner)
    credit_service.credit(
        channel.organization.account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"fund-{uuid.uuid4()}",
        idempotency_key=f"fund-{uuid.uuid4()}",
    )
    account = channel.organization.account
    queryset_cls = type(Account.objects.all())
    real_get = queryset_cls.get

    def get_negative_team(self, *args, **kwargs):
        row = real_get(self, *args, **kwargs)
        if row.pk == account.pk:
            row.balance = Decimal("-2.500000")
        return row

    monkeypatch.setattr(queryset_cls, "get", get_negative_team)

    summary = partner_summary_service.summarize(channel)

    monkeypatch.undo()
    assert summary.available_balance == Decimal("0.000000")
    account.refresh_from_db()
    assert account.balance == Decimal("20.000000")


@ENABLED
@pytest.mark.django_db
def test_unexpected_source_type_counts_in_total_and_earned() -> None:
    owner = _user("owner")
    channel = _channel_for(owner)
    _accrual(channel, source_type="order", partner_share="1.000000")
    _accrual(channel, source_type="legacy", partner_share="4.000000")

    summary = partner_summary_service.summarize(channel)

    assert summary.total_earned == Decimal("5.000000")
    assert summary.accrual_counts.order == 1
    assert summary.accrual_counts.topup == 0
    assert summary.accrual_counts.subscription == 0
    assert summary.accrual_counts.total == 2


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_returns_stored_history() -> None:
    owner = _user("owner")
    channel = _channel_for(owner, active=False)
    _accrual(channel, source_type="topup", partner_share="3.000000")

    summary = partner_summary_service.summarize(channel)

    assert summary.total_earned == Decimal("3.000000")
    assert summary.accrual_counts.topup == 1
    assert summary.accrual_counts.total == 1
