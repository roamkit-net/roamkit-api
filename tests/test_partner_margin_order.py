"""Order fulfillment partner-margin hook (ADR 023)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)
from apps.billing.services import credit_service
from apps.billing.services.partner_margin import (
    build_source_id,
    partner_margin_service,
)
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.services.order_service import OrderService
from apps.organizations.services.account_binding import create_organization
from shared.providers.esim import OrderedSimDTO, OrderResult

ENABLED = override_settings(BILLING_ENABLED=True, PARTNER_CHANNEL_ENABLED=True)


def _customer(email: str) -> User:
    return User.objects.create_user(email=email, password="secret123")


def _package(*, list_price: str, net_price: str | None) -> Package:
    return Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal(list_price),
        net_price_usd=None if net_price is None else Decimal(net_price),
        synced_at=timezone.now(),
    )


def _result() -> OrderResult:
    return OrderResult(
        external_order_id="9666",
        code="20230227-009666",
        package_id="pkg",
        customer_ref="ref",
        currency="USD",
        price_usd=Decimal("25.00"),
        manual_installation="",
        qrcode_installation="",
        installation_guide_url="",
        sims=[
            OrderedSimDTO(
                iccid=f"89{uuid.uuid4().int % 10**18:018d}"[:20],
                lpa="lpa.example",
                matching_id="MATCH",
                qrcode="LPA:1$lpa.example$MATCH",
                qrcode_url="https://example.test/qr",
                direct_apple_installation_url="https://example.test/apple",
            )
        ],
    )


class _Provider:
    def __init__(
        self, result: OrderResult | None = None, *, fail: bool = False
    ) -> None:
        self.result = result or _result()
        self.fail = fail
        self.calls = 0

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.result


def _fund(user: User, amount: str = "40.00") -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.DEPOSIT,
        reference_id=f"dep-{user.pk}",
        idempotency_key=f"dep-{user.pk}-{amount}",
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


def _fulfill(user: User, package: Package, provider: _Provider | None = None) -> Order:
    return OrderService(provider or _Provider()).fulfill(
        user=user,
        package=package,
        idempotency_key=f"order-{uuid.uuid4()}",
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, PARTNER_CHANNEL_ENABLED=False)
def test_flag_off_fulfills_without_accrual() -> None:
    user = _customer("flag-off@example.com")
    _fund(user)
    _channel(user, percent="50.00")

    order = _fulfill(user, _package(list_price="25.00", net_price="5.00"))

    assert order.status == Order.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()


@pytest.mark.django_db
@ENABLED
def test_no_attribution_fulfills_without_accrual() -> None:
    user = _customer("plain@example.com")
    _fund(user)

    order = _fulfill(user, _package(list_price="25.00", net_price="5.00"))

    assert order.status == Order.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_inactive_channel_fulfills_without_accrual() -> None:
    user = _customer("inactive@example.com")
    _fund(user)
    _channel(user, percent="50.00", active=False)

    order = _fulfill(user, _package(list_price="25.00", net_price="5.00"))

    assert order.status == Order.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_missing_net_fulfills_without_accrual() -> None:
    user = _customer("no-net@example.com")
    _fund(user)
    _channel(user, percent="50.00")

    order = _fulfill(user, _package(list_price="25.00", net_price=None))

    assert order.status == Order.Status.FULFILLED
    assert order.net_price_usd is None
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_order_accrual_uses_reserve_snapshot_not_later_catalog() -> None:
    user = _customer("earn@example.com")
    _fund(user)
    package = _package(list_price="25.00", net_price="5.00")
    channel = _channel(user, percent="50.00")
    result = _result()

    class _RewriteCatalog(_Provider):
        def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
            package.price_usd = Decimal("99.00")
            package.net_price_usd = Decimal("1.00")
            package.save(update_fields=["price_usd", "net_price_usd", "updated_at"])
            return super().create_order(package_id, customer_ref)

    order = _fulfill(user, package, _RewriteCatalog(result))

    assert order.status == Order.Status.FULFILLED
    order.refresh_from_db()
    assert order.list_price_usd == Decimal("25.00")
    assert order.net_price_usd == Decimal("5.00")
    source_id = build_source_id(
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_uuid=order.pk,
    )
    assert source_id == str(order.pk)
    accrual = PartnerMarginAccrual.objects.get()
    assert accrual.source_type == PartnerMarginAccrual.SourceType.ORDER
    assert accrual.source_id == source_id
    assert accrual.partner_channel_id == channel.pk
    assert accrual.customer_user_id == user.pk
    assert accrual.customer_user_id_snapshot == user.pk
    assert accrual.list_price == Decimal("25.000000")
    assert accrual.net_price == Decimal("5.000000")
    assert accrual.margin == Decimal("20.000000")
    assert accrual.partner_share == Decimal("10.000000")
    assert accrual.ledger_entry.reference_id == str(accrual.pk)
    assert accrual.ledger_entry.idempotency_key == f"partner-margin:order:{source_id}"
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")
    assert Esim.objects.filter(order=order).count() == 1


@pytest.mark.django_db
@ENABLED
def test_provider_failure_refunds_without_accrual() -> None:
    user = _customer("provider@example.com")
    _fund(user)
    _channel(user, percent="50.00")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _fulfill(
            user, _package(list_price="25.00", net_price="5.00"), _Provider(fail=True)
        )

    order = Order.objects.get()
    assert order.status == Order.Status.FAILED
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("40.000000")


@pytest.mark.django_db
@ENABLED
def test_partner_credit_failure_rolls_back_fulfilled(monkeypatch) -> None:
    user = _customer("boom@example.com")
    _fund(user)
    _channel(user, percent="50.00")

    def _boom(*args, **kwargs):
        raise RuntimeError("partner credit failed")

    monkeypatch.setattr(partner_margin_service._credits, "credit", _boom)
    with pytest.raises(RuntimeError, match="partner credit failed"):
        _fulfill(user, _package(list_price="25.00", net_price="5.00"))

    order = Order.objects.get()
    assert order.status == Order.Status.FULFILLING
    assert Esim.objects.count() == 0
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()
    debit = CreditLedgerEntry.objects.get(reference_type=LedgerReferenceType.ORDER)
    assert debit.delta == Decimal("-25.000000")
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("15.000000")


@pytest.mark.django_db
@ENABLED
def test_replay_of_fulfilled_order_does_not_accrue_again() -> None:
    user = _customer("replay@example.com")
    _fund(user)
    _channel(user, percent="50.00")
    package = _package(list_price="25.00", net_price="5.00")
    provider = _Provider()
    service = OrderService(provider)
    key = f"order-{uuid.uuid4()}"

    first = service.fulfill(user=user, package=package, idempotency_key=key)
    second = service.fulfill(user=user, package=package, idempotency_key=key)

    assert first.pk == second.pk
    assert provider.calls == 1
    assert PartnerMarginAccrual.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 1
    )
