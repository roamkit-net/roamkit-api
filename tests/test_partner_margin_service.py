"""Shared PartnerMarginService behavior for order, topup, and subscription."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    resolve_authorized_partner_context,
)
from apps.billing.services.partner_margin import (
    build_source_id,
    partner_channel_can_accrue_for_customer,
    partner_margin_service,
)
from apps.billing.services.partner_settlement import PartnerSettlementAccountMissing
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
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
def test_percent_change_before_lock_uses_the_new_value(
    source_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
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
    assert "reason=partner_margin.accrued" in caplog.text
    assert "partner_share=" not in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_missing_team_account_rolls_back_the_transaction(
    source_type: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    caplog.set_level(logging.ERROR, logger=LOGGER)

    with pytest.raises(PartnerSettlementAccountMissing):
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
    assert "reason=partner_margin.settlement_invalid" in caplog.text
    assert "error_type=PartnerSettlementAccountMissing" in caplog.text


def _individual(
    owner: User, *, percent: str = "50.00", active: bool = True
) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal(percent),
        is_active=active,
    )


def _membership(
    *,
    user: User,
    channel: PartnerChannel,
    role: str,
    status: str = MembershipStatus.ACTIVE,
) -> Membership:
    return Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=status,
    )


def _margin_rows() -> int:
    return CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).count()


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_individual_commission_credits_the_owner_personal_account(
    source_type: str,
) -> None:
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner, percent="33.33")
    _attribute(buyer, channel)
    personal = owner.billing_account
    profile_id = personal.pricing_profile_id
    buyer_profile_id = buyer.billing_account.pricing_profile_id
    kind = personal.kind
    accounts_before = Account.objects.count()

    accrual = _accrue(
        source_type=source_type,
        source_id=_source_id(source_type),
        customer=buyer,
    )

    assert accrual is not None
    personal.refresh_from_db()
    buyer.billing_account.refresh_from_db()
    assert accrual.partner_share == Decimal("6.666000")
    assert accrual.revenue_share_percent == Decimal("33.33")
    assert accrual.ledger_entry.account_id == personal.pk
    assert accrual.ledger_entry.delta == Decimal("6.666000")
    assert accrual.ledger_entry.idempotency_key.startswith("partner-margin:")
    assert personal.balance == Decimal("6.666000")
    assert personal.kind == kind
    assert personal.pricing_profile_id == profile_id
    assert buyer.billing_account.balance == Decimal("0")
    assert buyer.billing_account.pricing_profile_id == buyer_profile_id
    assert Account.objects.count() == accounts_before
    assert PartnerMarginAccrual.objects.count() == 1
    assert _margin_rows() == 1


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_individual_replay_does_not_double_credit(source_type: str) -> None:
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner)
    _attribute(buyer, channel)
    source_id = _source_id(source_type)

    first = _accrue(source_type=source_type, source_id=source_id, customer=buyer)
    second = _accrue(source_type=source_type, source_id=source_id, customer=buyer)

    assert first is not None
    assert second is not None
    assert second.pk == first.pk
    owner.billing_account.refresh_from_db()
    assert owner.billing_account.balance == Decimal("10.000000")
    assert PartnerMarginAccrual.objects.count() == 1
    assert _margin_rows() == 1


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_inactive_individual_writes_nothing_and_keeps_attribution(
    source_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner, active=False)
    attribution = _attribute(buyer, channel)

    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=buyer,
        )
        is None
    )

    attribution.refresh_from_db()
    owner.billing_account.refresh_from_db()
    assert attribution.partner_channel_id == channel.pk
    assert owner.billing_account.balance == Decimal("0")
    assert PartnerMarginAccrual.objects.count() == 0
    assert _margin_rows() == 0
    assert "reason=partner_margin.channel_inactive" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_owner_self_purchase_writes_nothing_and_keeps_attribution(
    source_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    owner = _customer()
    channel = _individual(owner)
    attribution = _attribute(owner, channel)
    assert partner_channel_can_accrue_for_customer(channel, owner) is False

    assert (
        _accrue(
            source_type=source_type,
            source_id=_source_id(source_type),
            customer=owner,
        )
        is None
    )

    attribution.refresh_from_db()
    owner.billing_account.refresh_from_db()
    assert attribution.partner_channel_id == channel.pk
    assert owner.billing_account.balance == Decimal("0")
    assert PartnerMarginAccrual.objects.count() == 0
    assert _margin_rows() == 0
    assert "reason=partner_margin.self_referral" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize("source_type", SOURCE_TYPES)
def test_missing_personal_settlement_account_rolls_back(
    source_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER)
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner)
    attribution = _attribute(buyer, channel)
    Account.objects.filter(user=owner).delete()
    accounts_before = Account.objects.count()

    with pytest.raises(PartnerSettlementAccountMissing):
        with transaction.atomic():
            locked = PartnerChannel.objects.select_for_update().get(pk=channel.pk)
            locked.revenue_share_percent = Decimal("12.50")
            locked.save(update_fields=["revenue_share_percent", "updated_at"])
            _accrue(
                source_type=source_type,
                source_id=_source_id(source_type),
                customer=buyer,
            )

    channel.refresh_from_db()
    attribution.refresh_from_db()
    assert channel.revenue_share_percent == Decimal("50.00")
    assert attribution.partner_channel_id == channel.pk
    assert not Account.objects.filter(user=owner).exists()
    assert Account.objects.count() == accounts_before
    assert PartnerMarginAccrual.objects.count() == 0
    assert _margin_rows() == 0
    assert "reason=partner_margin.settlement_invalid" in caplog.text
    assert "error_type=PartnerSettlementAccountMissing" in caplog.text


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
        MembershipRole.VIEWER,
        MembershipRole.MEMBER,
    ],
)
def test_active_team_membership_suppresses_margin(role: str) -> None:
    """Portal roles and the member role are the same economic guard.

    ``member`` has no partner-portal context. An active membership still
    suppresses commission.
    """
    buyer = _customer()
    channel = _channel(percent="50.00")
    if role == MembershipRole.OWNER:
        buyer = User.objects.get(
            pk=Membership.objects.get(
                organization=channel.organization,
                role=MembershipRole.OWNER,
            ).user_id
        )
    else:
        _membership(user=buyer, channel=channel, role=role)
    attribution = _attribute(buyer, channel)
    assert partner_channel_can_accrue_for_customer(channel, buyer) is False

    assert (
        _accrue(
            source_type=PartnerMarginAccrual.SourceType.ORDER,
            source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
            customer=buyer,
        )
        is None
    )

    attribution.refresh_from_db()
    channel.organization.account.refresh_from_db()
    assert attribution.partner_channel_id == channel.pk
    assert channel.organization.account.balance == Decimal("0")
    assert PartnerMarginAccrual.objects.count() == 0
    assert _margin_rows() == 0


@ENABLED
@pytest.mark.django_db
def test_active_member_has_no_portal_context_and_earns_no_margin() -> None:
    member = _customer()
    channel = _channel(percent="50.00")
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    attribution = _attribute(member, channel)

    with pytest.raises(PartnerAccessDenied):
        resolve_authorized_partner_context(member, channel.pk)
    assert partner_channel_can_accrue_for_customer(channel, member) is False
    assert (
        _accrue(
            source_type=PartnerMarginAccrual.SourceType.ORDER,
            source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
            customer=member,
        )
        is None
    )

    attribution.refresh_from_db()
    assert attribution.partner_channel_id == channel.pk
    assert PartnerMarginAccrual.objects.count() == 0
    assert _margin_rows() == 0


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "status_value",
    [MembershipStatus.SUSPENDED, MembershipStatus.REVOKED],
)
def test_inactive_membership_does_not_suppress_margin(status_value: str) -> None:
    buyer = _customer()
    channel = _channel(percent="50.00")
    _membership(
        user=buyer,
        channel=channel,
        role=MembershipRole.ADMIN,
        status=status_value,
    )
    _attribute(buyer, channel)
    assert partner_channel_can_accrue_for_customer(channel, buyer) is True

    accrual = _accrue(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
        customer=buyer,
    )

    assert accrual is not None
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")
    assert accrual.ledger_entry.account_id == channel.organization.account_id


@ENABLED
@pytest.mark.django_db
def test_team_settlement_account_is_the_organization_account() -> None:
    buyer = _customer()
    channel = _channel(percent="50.00")
    _attribute(buyer, channel)

    accrual = _accrue(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
        customer=buyer,
    )

    assert accrual is not None
    assert accrual.ledger_entry.account_id == channel.organization.account_id
    assert accrual.ledger_entry.idempotency_key == (
        f"partner-margin:{PartnerMarginAccrual.SourceType.ORDER}:{accrual.source_id}"
    )
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")
    assert channel.organization.account.kind == "organization"


@ENABLED
@pytest.mark.django_db
def test_replay_ignores_a_membership_added_after_success() -> None:
    buyer = _customer()
    channel = _channel(percent="50.00")
    _attribute(buyer, channel)
    source_id = _source_id(PartnerMarginAccrual.SourceType.ORDER)
    first = _accrue(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=source_id,
        customer=buyer,
    )
    _membership(user=buyer, channel=channel, role=MembershipRole.VIEWER)

    second = _accrue(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=source_id,
        customer=buyer,
    )

    assert first is not None
    assert second is not None
    assert second.pk == first.pk
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")
    assert PartnerMarginAccrual.objects.count() == 1


@ENABLED
@pytest.mark.django_db
def test_attribution_chooses_the_settlement_account() -> None:
    owner = _customer()
    individual = _individual(owner)
    team = _channel(percent="50.00")
    individual_buyer = _customer()
    team_buyer = _customer()
    _attribute(individual_buyer, individual)
    _attribute(team_buyer, team)

    individual_accrual = _accrue(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
        customer=individual_buyer,
    )
    team_accrual = _accrue(
        source_type=PartnerMarginAccrual.SourceType.TOPUP,
        source_id=_source_id(PartnerMarginAccrual.SourceType.TOPUP),
        customer=team_buyer,
    )

    assert individual_accrual is not None
    assert team_accrual is not None
    owner.billing_account.refresh_from_db()
    team.organization.account.refresh_from_db()
    assert individual_accrual.ledger_entry.account_id == owner.billing_account.pk
    assert team_accrual.ledger_entry.account_id == team.organization.account_id
    assert owner.billing_account.balance == Decimal("10.000000")
    assert team.organization.account.balance == Decimal("10.000000")
    assert owner.billing_account.pk != team.organization.account_id


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_two_commissions_credit_one_personal_account() -> None:
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner)
    _attribute(buyer, channel)
    first_source = _source_id(PartnerMarginAccrual.SourceType.ORDER)
    second_source = _source_id(PartnerMarginAccrual.SourceType.ORDER)

    def _once(source_id: str) -> str:
        try:
            with transaction.atomic():
                accrual = partner_margin_service.accrue(
                    source_type=PartnerMarginAccrual.SourceType.ORDER,
                    source_id=source_id,
                    list_price=Decimal("25.00"),
                    net_price=Decimal("5.00"),
                    customer=buyer,
                )
            return "ok" if accrual is not None else "skip"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_once, source_id) for source_id in (first_source, second_source)
        ]
        assert [future.result(timeout=30) for future in futures] == ["ok", "ok"]

    owner.billing_account.refresh_from_db()
    assert owner.billing_account.balance == Decimal("20.000000")
    assert PartnerMarginAccrual.objects.count() == 2
    assert _margin_rows() == 2


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_personal_debit_and_commission_credit_serialize() -> None:
    owner = _customer()
    buyer = _customer()
    channel = _individual(owner)
    _attribute(buyer, channel)
    personal = owner.billing_account
    credit_service.credit(
        personal,
        Decimal("10.000000"),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id=f"fund-{uuid.uuid4()}",
        idempotency_key=f"fund-{uuid.uuid4()}",
    )
    personal_id = personal.pk

    def _debit() -> str:
        try:
            credit_service.debit(
                personal,
                Decimal("10.000000"),
                reference_type=LedgerReferenceType.ORDER,
                reference_id=f"order-{uuid.uuid4()}",
                idempotency_key="purchase-race",
            )
            return "ok"
        finally:
            connection.close()

    def _commission() -> str:
        try:
            with transaction.atomic():
                accrual = partner_margin_service.accrue(
                    source_type=PartnerMarginAccrual.SourceType.ORDER,
                    source_id=_source_id(PartnerMarginAccrual.SourceType.ORDER),
                    list_price=Decimal("25.00"),
                    net_price=Decimal("5.00"),
                    customer=buyer,
                )
            return "ok" if accrual is not None else "skip"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_debit), pool.submit(_commission)]
        assert sorted(future.result(timeout=30) for future in futures) == ["ok", "ok"]

    personal = Account.objects.get(pk=personal_id)
    assert personal.balance == Decimal("10.000000")
    assert (
        CreditLedgerEntry.objects.filter(
            account_id=personal_id,
            reference_type=LedgerReferenceType.ORDER,
        ).count()
        == 1
    )
    assert (
        CreditLedgerEntry.objects.filter(
            account_id=personal_id,
            reference_type=LedgerReferenceType.PARTNER_MARGIN,
        ).count()
        == 1
    )
