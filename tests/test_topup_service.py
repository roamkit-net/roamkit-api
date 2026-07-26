"""Tests for TopupService prepaid purchase flow."""

from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.exceptions import InsufficientFundsError
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Package
from apps.esims.exceptions import TopupPackageNotFoundError
from apps.esims.models import Esim, Topup
from apps.esims.services.topup_service import TopupService
from apps.orders.exceptions import ProviderFulfillmentError
from apps.orders.models import Order
from shared.events.billing_events import CreditDebited, FulfillmentRefunded
from shared.events.event_bus import event_bus
from shared.events.order_events import TopupCompleted
from shared.providers.esim import TopupPackage, TopupResult, UsageDTO


class FakeTopupProvider:
    def __init__(
        self,
        *,
        topups: list[TopupPackage] | None = None,
        result: TopupResult | None = None,
        fail: bool = False,
    ) -> None:
        self.topups = topups or [
            TopupPackage(
                external_id="topup-1gb",
                title="1 GB Top-up",
                data_allowance="1 GB",
                validity_days=7,
                price_usd=Decimal("5.00"),
                net_price_usd=Decimal("4.50"),
                is_unlimited=False,
                plan_type="topup",
            )
        ]
        self.result = result
        self.fail = fail
        self.submit_calls: list[tuple[str, str]] = []

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        self.submit_calls.append((iccid, package_id))
        if self.fail:
            raise RuntimeError("provider unavailable")
        assert self.result is not None
        return self.result

    def get_usage(self, iccid: str) -> UsageDTO:
        raise AssertionError("get_usage not used in these tests")


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="topup@example.com", password="secret123")


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def esim(user: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-1",
        customer_ref="ref-1",
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000009125",
        status=Esim.Status.ACTIVATED,
    )


@pytest.fixture
def topup_result(esim: Esim) -> TopupResult:
    return TopupResult(
        external_order_id="top-99",
        code="TOP-99",
        package_id="topup-1gb",
        iccid=esim.iccid,
        currency="USD",
        price_usd=Decimal("5.00"),
        customer_ref="ref",
    )


def _fund(user: User, amount: str = "10.00") -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="test-fund",
        idempotency_key=f"fund:{user.pk}:{amount}",
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_debits_and_fulfills(
    user: User, esim: Esim, topup_result: TopupResult
) -> None:
    _fund(user, "10.00")
    completed: list[TopupCompleted] = []
    debited: list[CreditDebited] = []
    event_bus.subscribe(TopupCompleted, completed.append)
    event_bus.subscribe(CreditDebited, debited.append)

    provider = FakeTopupProvider(result=topup_result)
    topup = TopupService(provider).purchase(
        esim, package_id="topup-1gb", idempotency_key="topup-pay-1"
    )

    assert topup.status == Topup.Status.FULFILLED
    assert topup.external_order_id == "top-99"
    assert topup.amount == Decimal("5.00")
    assert provider.submit_calls == [(esim.iccid, "topup-1gb")]

    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("5.000000")
    assert CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.TOPUP,
        reference_id=str(topup.pk),
    ).exists()
    assert len(completed) == 1
    assert completed[0].topup_id == str(topup.pk)
    assert len(debited) == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_unknown_package(user: User, esim: Esim) -> None:
    _fund(user, "10.00")
    with pytest.raises(TopupPackageNotFoundError):
        TopupService(FakeTopupProvider()).purchase(
            esim, package_id="missing", idempotency_key="topup-missing"
        )
    assert Topup.objects.count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_insufficient_funds(user: User, esim: Esim) -> None:
    with pytest.raises(InsufficientFundsError):
        TopupService(FakeTopupProvider(result=None)).purchase(
            esim, package_id="topup-1gb", idempotency_key="topup-underfunded"
        )
    assert Topup.objects.count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_refunds_on_provider_failure(user: User, esim: Esim) -> None:
    _fund(user, "10.00")
    refunded: list[FulfillmentRefunded] = []
    event_bus.subscribe(FulfillmentRefunded, refunded.append)

    with pytest.raises(ProviderFulfillmentError, match="Provider fulfillment failed"):
        TopupService(FakeTopupProvider(fail=True)).purchase(
            esim, package_id="topup-1gb", idempotency_key="topup-fail-1"
        )

    topup = Topup.objects.get()
    assert topup.status == Topup.Status.FAILED
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("10.000000")
    assert CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.REFUND,
        reference_id=str(topup.pk),
    ).exists()
    assert len(refunded) == 1
    assert refunded[0].reference_type == LedgerReferenceType.TOPUP


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_purchase_is_idempotent_on_key(
    user: User, esim: Esim, topup_result: TopupResult
) -> None:
    _fund(user, "10.00")
    provider = FakeTopupProvider(result=topup_result)
    service = TopupService(provider)

    first = service.purchase(
        esim, package_id="topup-1gb", idempotency_key="topup-idem-1"
    )
    second = service.purchase(
        esim, package_id="topup-1gb", idempotency_key="topup-idem-1"
    )

    assert first.pk == second.pk
    assert Topup.objects.count() == 1
    assert len(provider.submit_calls) == 1
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("5.000000")
