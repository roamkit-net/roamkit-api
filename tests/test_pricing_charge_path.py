"""PR3 charge path — resolve once → snapshot → debit/refund from snapshot."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Location, Package
from apps.orders.models import Order
from apps.orders.services.order_service import OrderService
from apps.pricing.models import FloorPolicy, PricingProfile
from shared.providers.esim import OrderedSimDTO, OrderResult

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class FakeOrderProvider:
    def __init__(self, result: OrderResult, *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls = 0

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return self.result


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="charge@example.com", password="x")


@pytest.fixture
def location(db) -> Location:
    return Location.objects.create(
        slug="hr-charge",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )


@pytest.fixture
def package(db, location: Location) -> Package:
    return Package.objects.create(
        external_id="charge-7days-1gb",
        title="1 GB - 7 days",
        operator_title="Cronet",
        country_code="HR",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("56.00"),
        net_price_usd=Decimal("50.00"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def order_result(package: Package) -> OrderResult:
    return OrderResult(
        external_order_id="ext-charge-1",
        code="code-1",
        package_id=package.external_id,
        customer_ref="rk-test",
        currency="USD",
        price_usd=Decimal("50.00"),
        manual_installation="",
        qrcode_installation="",
        installation_guide_url="",
        sims=[
            OrderedSimDTO(
                iccid="8910300000063703999",
                lpa="consumer.e-sim.global",
                matching_id="TNTEST",
                qrcode="LPA:1$x$TNTEST",
                qrcode_url="https://example.com/qr",
                direct_apple_installation_url="",
            )
        ],
    )


def _fund(user: User, amount: str = "100.00") -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund-charge",
        idempotency_key=f"fund-charge:{user.pk}:{amount}",
    )


@pytest.mark.django_db
def test_flag_off_debits_list_price(
    settings, user: User, package: Package, order_result: OrderResult
) -> None:
    settings.PRICING_PROFILES_ENABLED = False
    _fund(user)
    order = OrderService(FakeOrderProvider(order_result)).fulfill(
        user=user,
        package=package,
        idempotency_key="charge-legacy-1",
    )
    assert order.retail_price_usd == Decimal("56.00")
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("44.00")


@pytest.mark.django_db
def test_flag_on_debits_customer_price_with_snapshot(
    settings, user: User, package: Package, order_result: OrderResult
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family-charge",
        discount_percent=Decimal("10.00"),
        floor_policy=FloorPolicy.WHOLESALE,
    )
    account = user.billing_account
    account.pricing_profile = profile
    account.save(update_fields=["pricing_profile", "updated_at"])
    _fund(user)

    order = OrderService(FakeOrderProvider(order_result)).fulfill(
        user=user,
        package=package,
        idempotency_key="charge-disc-1",
    )
    # margin-share: L=56 N=50 D=10% → C = 50 + 5.40 = 55.40
    assert order.list_price_usd == Decimal("56.00")
    assert order.retail_price_usd == Decimal("55.40")
    assert order.pricing_profile_slug == "family-charge"
    assert order.pricing_context_hash
    assert order.snapshot_schema_version == 1

    debit = CreditLedgerEntry.objects.get(
        reference_type=LedgerReferenceType.ORDER,
        reference_id=str(order.pk),
    )
    assert debit.delta == Decimal("-55.40")
    account.refresh_from_db()
    assert account.balance == Decimal("44.60")


@pytest.mark.django_db
def test_refund_uses_snapshot_not_live_profile(
    settings, user: User, package: Package, order_result: OrderResult
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family-refund",
        discount_percent=Decimal("10.00"),
        floor_policy=FloorPolicy.WHOLESALE,
    )
    account = user.billing_account
    account.pricing_profile = profile
    account.save(update_fields=["pricing_profile", "updated_at"])
    _fund(user)

    provider = FakeOrderProvider(order_result, fail=True)
    with pytest.raises(RuntimeError, match="provider down"):
        OrderService(provider).fulfill(
            user=user,
            package=package,
            idempotency_key="charge-refund-1",
        )

    order = Order.objects.get(idempotency_key="charge-refund-1")
    assert order.status == Order.Status.FAILED
    assert order.retail_price_usd == Decimal("55.40")

    # Change profile after failure — refund must still be 55.40 from snapshot.
    profile.discount_percent = Decimal("50.00")
    profile.save()

    refund = CreditLedgerEntry.objects.get(
        reference_type=LedgerReferenceType.REFUND,
        reference_id=str(order.pk),
    )
    assert refund.delta == Decimal("55.40")
    account.refresh_from_db()
    assert account.balance == Decimal("100.00")


@pytest.mark.django_db
def test_replay_does_not_double_debit(
    settings, user: User, package: Package, order_result: OrderResult
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    _fund(user)
    svc = OrderService(FakeOrderProvider(order_result))
    first = svc.fulfill(user=user, package=package, idempotency_key="charge-replay-1")
    second = svc.fulfill(user=user, package=package, idempotency_key="charge-replay-1")
    assert first.pk == second.pk
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.ORDER,
            reference_id=str(first.pk),
        ).count()
        == 1
    )


def test_compensate_never_calls_pricing_resolve():
    """Architecture: refund path must not re-resolve price."""
    path = SRC_ROOT / "apps" / "orders" / "services" / "order_service.py"
    text = path.read_text(encoding="utf-8")
    # Split on compensate method
    idx = text.index("def _compensate_failed")
    compensate = text[idx : text.index("def _persist_fulfillment", idx)]
    assert "resolve_package_charge" not in compensate
    assert "pricing_service" not in compensate
    assert ".resolve(" not in compensate


def test_resolve_only_in_reserve_path():
    """OrderService may resolve only inside _reserve_and_debit (once per new order)."""
    path = SRC_ROOT / "apps" / "orders" / "services" / "order_service.py"
    text = path.read_text(encoding="utf-8")
    # import + single call site
    assert text.count("resolve_package_charge") == 2
    call_region = text[text.index("def _reserve_and_debit") :]
    assert call_region.count("resolve_package_charge(") == 1
    assert "resolve_package_charge(" not in text[text.index("def _compensate_failed") :]


def test_topup_compensate_never_re_resolves():
    path = SRC_ROOT / "apps" / "esims" / "services" / "topup_service.py"
    text = path.read_text(encoding="utf-8")
    idx = text.index("def _compensate_failed")
    compensate = text[idx : text.index("def _persist_fulfillment", idx)]
    assert "resolve_topup_charge" not in compensate
    assert "pricing_service" not in compensate
    assert text.count("resolve_topup_charge(") == 1
