"""Top-up fulfillment partner-margin hook (ADR 023)."""

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
from apps.esims.models import Esim, Topup
from apps.esims.services.topup_service import TopupService
from apps.orders.exceptions import ProviderFulfillmentError
from apps.orders.models import Order
from apps.organizations.services.account_binding import create_organization
from shared.events.event_bus import event_bus
from shared.events.order_events import TopupCompleted
from shared.providers.esim import TopupPackage, TopupResult

ENABLED = override_settings(BILLING_ENABLED=True, PARTNER_CHANNEL_ENABLED=True)


def _customer(email: str) -> User:
    return User.objects.create_user(email=email, password="secret123")


def _esim(user: User) -> Esim:
    package = Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="1 GB",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
    )
    return Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid=f"89{uuid.uuid4().int % 10**18:018d}"[:20],
        status=Esim.Status.ACTIVATED,
    )


def _topup_package(*, list_price: str, net_price: str | None) -> TopupPackage:
    return TopupPackage(
        external_id="topup-1gb",
        title="1 GB Top-up",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal(list_price),
        net_price_usd=None if net_price is None else Decimal(net_price),
        is_unlimited=False,
        plan_type="topup",
    )


def _result(esim: Esim) -> TopupResult:
    return TopupResult(
        external_order_id="top-99",
        code="TOP-99",
        package_id="topup-1gb",
        iccid=esim.iccid,
        currency="USD",
        price_usd=Decimal("25.00"),
        customer_ref="ref",
    )


class _Provider:
    def __init__(
        self,
        package: TopupPackage,
        result: TopupResult,
        *,
        fail: bool = False,
    ) -> None:
        self.topups = [package]
        self.result = result
        self.fail = fail
        self.submit_calls = 0

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        self.submit_calls += 1
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


def _purchase(
    user: User,
    *,
    list_price: str = "25.00",
    net_price: str | None = "5.00",
    provider: _Provider | None = None,
    key: str | None = None,
) -> Topup:
    esim = user.billing_account.esims.get()
    package = _topup_package(list_price=list_price, net_price=net_price)
    chosen = provider or _Provider(package, _result(esim))
    return TopupService(chosen).purchase(
        esim,
        package_id="topup-1gb",
        idempotency_key=key or f"topup-{uuid.uuid4()}",
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, PARTNER_CHANNEL_ENABLED=False)
def test_flag_off_fulfills_without_accrual() -> None:
    user = _customer("flag-off@example.com")
    _esim(user)
    _fund(user)
    _channel(user, percent="50.00")

    topup = _purchase(user)

    assert topup.status == Topup.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()


@pytest.mark.django_db
@ENABLED
def test_no_attribution_fulfills_without_accrual() -> None:
    user = _customer("plain@example.com")
    _esim(user)
    _fund(user)

    topup = _purchase(user)

    assert topup.status == Topup.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_inactive_channel_fulfills_without_accrual() -> None:
    user = _customer("inactive@example.com")
    _esim(user)
    _fund(user)
    _channel(user, percent="50.00", active=False)

    topup = _purchase(user)

    assert topup.status == Topup.Status.FULFILLED
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_missing_net_fulfills_without_accrual() -> None:
    user = _customer("no-net@example.com")
    _esim(user)
    _fund(user)
    _channel(user, percent="50.00")

    topup = _purchase(user, net_price=None)

    assert topup.status == Topup.Status.FULFILLED
    assert topup.net_price_usd is None
    assert PartnerMarginAccrual.objects.count() == 0


@pytest.mark.django_db
@ENABLED
def test_topup_accrual_uses_row_snapshot_not_later_package() -> None:
    user = _customer("earn@example.com")
    esim = _esim(user)
    _fund(user)
    channel = _channel(user, percent="50.00")
    package = _topup_package(list_price="25.00", net_price="5.00")

    class _RewritePackage(_Provider):
        def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
            self.topups = [_topup_package(list_price="99.00", net_price="1.00")]
            return super().submit_topup(iccid, package_id)

    topup = _purchase(user, provider=_RewritePackage(package, _result(esim)))

    assert topup.status == Topup.Status.FULFILLED
    assert topup.external_order_id == "top-99"
    topup.refresh_from_db()
    assert topup.list_price_usd == Decimal("25.00")
    assert topup.net_price_usd == Decimal("5.000000")
    source_id = build_source_id(
        source_type=PartnerMarginAccrual.SourceType.TOPUP,
        source_uuid=topup.pk,
    )
    assert source_id == str(topup.pk)
    accrual = PartnerMarginAccrual.objects.get()
    assert accrual.source_type == PartnerMarginAccrual.SourceType.TOPUP
    assert accrual.source_id == source_id
    assert accrual.partner_channel_id == channel.pk
    assert accrual.customer_user_id == user.pk
    assert accrual.customer_user_id_snapshot == user.pk
    assert accrual.list_price == Decimal("25.000000")
    assert accrual.net_price == Decimal("5.000000")
    assert accrual.margin == Decimal("20.000000")
    assert accrual.partner_share == Decimal("10.000000")
    assert accrual.ledger_entry.reference_id == str(accrual.pk)
    assert accrual.ledger_entry.idempotency_key == f"partner-margin:topup:{source_id}"
    channel.organization.account.refresh_from_db()
    assert channel.organization.account.balance == Decimal("10.000000")


@pytest.mark.django_db
@ENABLED
def test_provider_failure_refunds_without_accrual() -> None:
    user = _customer("provider@example.com")
    esim = _esim(user)
    _fund(user)
    _channel(user, percent="50.00")
    package = _topup_package(list_price="25.00", net_price="5.00")

    with pytest.raises(ProviderFulfillmentError):
        _purchase(user, provider=_Provider(package, _result(esim), fail=True))

    topup = Topup.objects.get()
    assert topup.status == Topup.Status.FAILED
    assert PartnerMarginAccrual.objects.count() == 0
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("40.000000")


@pytest.mark.django_db
@ENABLED
def test_partner_credit_failure_rolls_back_fulfilled(monkeypatch) -> None:
    user = _customer("boom@example.com")
    _esim(user)
    _fund(user)
    _channel(user, percent="50.00")
    completed: list[TopupCompleted] = []
    event_bus.subscribe(TopupCompleted, completed.append)

    def _boom(*args, **kwargs):
        raise RuntimeError("partner credit failed")

    monkeypatch.setattr(partner_margin_service._credits, "credit", _boom)
    with pytest.raises(RuntimeError, match="partner credit failed"):
        _purchase(user)

    topup = Topup.objects.get()
    assert topup.status == Topup.Status.FULFILLING
    assert topup.external_order_id == ""
    assert PartnerMarginAccrual.objects.count() == 0
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_MARGIN
    ).exists()
    debit = CreditLedgerEntry.objects.get(reference_type=LedgerReferenceType.TOPUP)
    assert debit.delta == Decimal("-25.000000")
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("15.000000")
    assert completed == []


@pytest.mark.django_db
@ENABLED
def test_replay_of_fulfilled_topup_does_not_accrue_again() -> None:
    user = _customer("replay@example.com")
    esim = _esim(user)
    _fund(user)
    _channel(user, percent="50.00")
    package = _topup_package(list_price="25.00", net_price="5.00")
    provider = _Provider(package, _result(esim))
    service = TopupService(provider)
    key = f"topup-{uuid.uuid4()}"

    first = service.purchase(esim, package_id="topup-1gb", idempotency_key=key)
    second = service.purchase(esim, package_id="topup-1gb", idempotency_key=key)

    assert first.pk == second.pk
    assert provider.submit_calls == 1
    assert PartnerMarginAccrual.objects.count() == 1
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_MARGIN
        ).count()
        == 1
    )
