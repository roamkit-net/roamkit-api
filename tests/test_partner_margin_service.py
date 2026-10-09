"""Shared PartnerMarginService behavior for order, topup, and subscription."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.partner_margin import (
    PartnerChannelTeamAccountMissing,
    build_source_id,
    partner_margin_service,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True)
BILLING_DATE = date(2026, 10, 8)
LOGGER = "apps.billing.services.partner_margin"
SOURCE_TYPES = [
    PartnerMarginAccrual.SourceType.ORDER,
    PartnerMarginAccrual.SourceType.TOPUP,
    PartnerMarginAccrual.SourceType.SUBSCRIPTION,
]


def _customer(email: str | None = None) -> User:
    return User.objects.create_user(
        email=email or f"buyer-{uuid.uuid4()}@example.com",
        password="secret123",
    )


def _channel(*, percent: str, active: bool = True) -> PartnerChannel:
    owner = _customer(f"owner-{uuid.uuid4()}@example.com")
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal(percent),
        is_active=active,
    )


def _attribute(user: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


def _source_id(source_type: str) -> str:
    if source_type == PartnerMarginAccrual.SourceType.ORDER:
        return build_source_id(source_type=source_type, source_uuid=uuid.uuid4().int)
    if source_type == PartnerMarginAccrual.SourceType.SUBSCRIPTION:
        return build_source_id(
            source_type=source_type,
            source_uuid=uuid.uuid4(),
            billing_date=BILLING_DATE,
        )
    return build_source_id(source_type=source_type, source_uuid=uuid.uuid4())


def _accrue(
    *,
    source_type: str,
    source_id: str,
    customer: User | None,
    list_price: str | None = "25.00",
    net_price: str | None = "5.00",
):
    return partner_margin_service.accrue(
        source_type=source_type,
        source_id=source_id,
        list_price=None if list_price is None else Decimal(list_price),
        net_price=None if net_price is None else Decimal(net_price),
        customer=customer,
    )


def test_build_source_id_uses_each_models_primary_key() -> None:
    order_id = 4821
    topup_id = uuid.UUID("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
    subscription_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

    assert (
        build_source_id(
            source_type=PartnerMarginAccrual.SourceType.ORDER,
            source_uuid=order_id,
        )
        == "4821"
    )
    assert (
        build_source_id(
            source_type=PartnerMarginAccrual.SourceType.TOPUP,
            source_uuid=topup_id,
        )
        == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    assert (
        build_source_id(
            source_type=PartnerMarginAccrual.SourceType.SUBSCRIPTION,
            source_uuid=subscription_id,
            billing_date=BILLING_DATE,
        )
        == "12345678-1234-5678-1234-567812345678:2026-10-08"
    )
    with pytest.raises(ValueError, match="billing_date"):
        build_source_id(
            source_type=PartnerMarginAccrual.SourceType.SUBSCRIPTION,
            source_uuid=subscription_id,
        )


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
@pytest.mark.parametrize(
    ("percent", "list_price", "net_price", "expected_share"),
    [
        ("50.00", "25.00", "5.00", "10.000000"),
        ("33.33", "25.00", "5.00", "6.666000"),
        ("12.50", "25.00", "5.00", "2.500000"),
        ("50.00", "1.000000", "0.999999", "0.000001"),
    ],
)
def test_accrual_formula_and_single_ledger_row(
    source_type: str,
    percent: str,
    list_price: str,
    net_price: str,
    expected_share: str,
) -> None:
    user = _customer()
    channel = _channel(percent=percent)
    _attribute(user, channel)
    source_id = _source_id(source_type)

    accrual = _accrue(
        source_type=source_type,
        source_id=source_id,
        customer=user,
        list_price=list_price,
        net_price=net_price,
    )

    assert accrual is not None
    assert PartnerMarginAccrual.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 1
    )
    assert accrual.source_type == source_type
    assert accrual.source_id == source_id
    assert accrual.partner_channel_id == channel.pk
    assert accrual.customer_user_id == user.pk
    assert accrual.customer_user_id_snapshot == user.pk
    assert accrual.revenue_share_percent == Decimal(percent)
    assert accrual.partner_share == Decimal(expected_share)
    assert accrual.margin == Decimal(list_price) - Decimal(net_price)
    entry = accrual.ledger_entry
    assert entry.reference_type == LedgerReferenceType.PARTNER_MARGIN
    assert entry.reference_id == str(accrual.pk)
    assert entry.idempotency_key == f"partner-margin:{source_type}:{source_id}"
    assert entry.delta == Decimal(expected_share)
    assert entry.account_id == channel.organization.account_id
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal(expected_share)


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
@pytest.mark.parametrize(
    ("list_price", "net_price", "percent", "reason"),
    [
        ("25.00", None, "50.00", "partner_margin.net_missing"),
        ("25.00", "-1.00", "50.00", "partner_margin.net_negative"),
        ("5.00", "10.00", "50.00", "partner_margin.invalid_margin"),
        ("25.00", "5.00", "0.00", "partner_margin.zero_share"),
        ("10.00", "10.00", "50.00", "partner_margin.zero_share"),
        ("1.000000", "0.999999", "0.01", "partner_margin.zero_share"),
    ],
)
def test_price_skips_write_nothing(
    source_type: str,
    list_price: str | None,
    net_price: str | None,
    percent: str,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    user = _customer()
    _attribute(user, _channel(percent=percent))
    source_id = _source_id(source_type)

    assert (
        _accrue(
            source_type=source_type,
            source_id=source_id,
            customer=user,
            list_price=list_price,
            net_price=net_price,
        )
        is None
    )
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()
    assert f"reason={reason}" in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
@override_settings(PARTNER_CHANNEL_ENABLED=False)
def test_flag_off_skips(source_type: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    user = _customer()
    _attribute(user, _channel(percent="50.00"))

    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=user,
        )
        is None
    )
    assert PartnerMarginAccrual.objects.count() == 0
    assert "reason=partner_margin.flag_disabled" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_missing_attribution_skips(
    source_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    user = _customer()

    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=user,
        )
        is None
    )
    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=None,
        )
        is None
    )
    assert PartnerMarginAccrual.objects.count() == 0
    assert caplog.text.count("reason=partner_margin.no_attribution") == 2


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_inactive_channel_skips(
    source_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    user = _customer()
    _attribute(user, _channel(percent="50.00", active=False))

    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=user,
        )
        is None
    )
    assert PartnerMarginAccrual.objects.count() == 0
    assert "reason=partner_margin.channel_inactive" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_repeat_source_returns_the_same_accrual_and_ledger(
    source_type: str,
) -> None:
    user = _customer()
    channel = _channel(percent="50.00")
    _attribute(user, channel)
    source_id = _source_id(source_type)

    first = _accrue(source_type=source_type, source_id=source_id, customer=user)
    second = _accrue(source_type=source_type, source_id=source_id, customer=user)

    assert first is not None
    assert second is not None
    assert second.pk == first.pk
    assert second.ledger_entry_id == first.ledger_entry_id
    assert PartnerMarginAccrual.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 1
    )
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_attribution_change_before_accrual_credits_the_new_partner(
    source_type: str,
) -> None:
    user = _customer()
    previous = _channel(percent="50.00")
    current = _channel(percent="50.00")
    attribution = _attribute(user, previous)
    attribution.partner_channel = current
    attribution.save(update_fields=["partner_channel"])

    accrual = _accrue(
        source_type=source_type,
        source_id=_source_id(source_type),
        customer=user,
    )

    assert accrual is not None
    assert accrual.partner_channel_id == current.pk
    assert accrual.customer_attribution_id == attribution.pk
    previous.organization.account.refresh_from_db()
    current.organization.account.refresh_from_db()
    assert previous.organization.account.balance == Decimal("0")
    assert current.organization.account.balance == Decimal("10.000000")


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_percent_change_before_lock_uses_the_new_value(source_type: str) -> None:
    user = _customer()
    channel = _channel(percent="50.00")
    _attribute(user, channel)
    channel.revenue_share_percent = Decimal("33.33")
    channel.save(update_fields=["revenue_share_percent", "updated_at"])

    accrual = _accrue(
        source_type=source_type,
        source_id=_source_id(source_type),
        customer=user,
    )

    assert accrual is not None
    assert accrual.revenue_share_percent == Decimal("33.33")
    assert accrual.partner_share == Decimal("6.666000")


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_missing_team_account_rolls_back_the_transaction(
    source_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _customer()
    channel = _channel(percent="50.00")
    _attribute(user, channel)
    queryset_cls = type(PartnerChannel.objects.all())
    real_get = queryset_cls.get

    def get_without_team_account(self, *args, **kwargs):
        row = real_get(self, *args, **kwargs)
        organization = row.organization
        organization._state.fields_cache.pop("account", None)
        organization.account_id = uuid.uuid4()
        return row

    monkeypatch.setattr(queryset_cls, "get", get_without_team_account)

    with pytest.raises(PartnerChannelTeamAccountMissing):
        with transaction.atomic():
            locked = PartnerChannel.objects.select_for_update().get(pk=channel.pk)
            locked.revenue_share_percent = Decimal("12.50")
            locked.save(update_fields=["revenue_share_percent", "updated_at"])
            _accrue(
                source_type=source_type,
                source_id=_source_id(source_type),
                customer=user,
            )

    channel.refresh_from_db()
    assert channel.revenue_share_percent == Decimal("50.00")
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()
