"""PricingService + snapshot helpers (ADR 019 PR2) — no debit wiring."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import Account
from apps.orders.models import Order
from apps.pricing.models import FloorPolicy, PricingProfile
from apps.pricing.money import money_round
from apps.pricing.service import pricing_service
from apps.pricing.snapshot import (
    quote_from_order,
    quote_to_order_snapshot_kwargs,
    quote_to_topup_snapshot_kwargs,
)
from apps.pricing.types import (
    SNAPSHOT_SCHEMA_VERSION,
    FloorReason,
    OrderType,
    PricingContext,
    PricingReason,
)

User = get_user_model()
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def user(db):
    return User.objects.create_user(email="price-svc@example.com", password="x")


@pytest.fixture
def account(user) -> Account:
    return Account.objects.get(user=user)


@pytest.fixture
def family_profile(db) -> PricingProfile:
    return PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("10.00"),
        floor_policy=FloorPolicy.WHOLESALE,
    )


def _ctx(
    *,
    list_price="56.00",
    net_price="50.00",
    profile=None,
    account=None,
    ts=None,
    order_type=OrderType.PACKAGE,
) -> PricingContext:
    return PricingContext(
        list_price=Decimal(list_price),
        net_price=Decimal(net_price) if net_price is not None else None,
        order_type=order_type,
        timestamp=ts or timezone.now(),
        account=account,
        profile=profile,
    )


@pytest.mark.django_db
def test_money_round_half_up():
    assert money_round(Decimal("1.005")) == Decimal("1.01")
    assert money_round(Decimal("1.004")) == Decimal("1.00")


@pytest.mark.django_db
def test_flag_off_always_retail(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = False
    q = pricing_service.resolve(_ctx(profile=family_profile))
    assert q.customer_price == Decimal("56.00")
    assert q.pricing_reason == PricingReason.RETAIL
    assert q.floor_reason == FloorReason.NONE
    assert q.pricing_profile_id is None


@pytest.mark.django_db
def test_discount_without_floor_hit(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    # 10% off 56 = 50.40 > net 50
    q = pricing_service.resolve(_ctx(profile=family_profile))
    assert q.customer_price == Decimal("50.40")
    assert q.pricing_reason == PricingReason.PRICING_PROFILE
    assert q.floor_reason == FloorReason.DISCOUNT
    assert q.pricing_profile_id == family_profile.pk
    assert q.pricing_profile_version == family_profile.version
    assert q.snapshot_schema_version == SNAPSHOT_SCHEMA_VERSION


@pytest.mark.django_db
def test_wholesale_floor_hit(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    family_profile.discount_percent = Decimal("20.00")
    family_profile.save()
    # 20% off 56 = 44.80 < net 50 → floor 50
    q = pricing_service.resolve(_ctx(profile=family_profile))
    assert q.customer_price == Decimal("50.00")
    assert q.floor_reason == FloorReason.WHOLESALE_FLOOR


@pytest.mark.django_db
def test_floor_policy_none(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    family_profile.discount_percent = Decimal("20.00")
    family_profile.floor_policy = FloorPolicy.NONE
    family_profile.save()
    q = pricing_service.resolve(_ctx(profile=family_profile))
    assert q.customer_price == Decimal("44.80")
    assert q.floor_reason == FloorReason.DISCOUNT


@pytest.mark.django_db
def test_outside_effective_window_retail(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    now = timezone.now()
    family_profile.effective_from = now + timedelta(days=1)
    family_profile.save()
    q = pricing_service.resolve(_ctx(profile=family_profile, ts=now))
    assert q.pricing_reason == PricingReason.RETAIL
    assert q.customer_price == Decimal("56.00")


@pytest.mark.django_db
def test_archived_profile_retail(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    family_profile.archive()
    q = pricing_service.resolve(_ctx(profile=family_profile))
    assert q.pricing_reason == PricingReason.RETAIL


@pytest.mark.django_db
def test_resolve_deterministic(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    ts = timezone.now()
    ctx = _ctx(profile=family_profile, ts=ts)
    a = pricing_service.resolve(ctx)
    b = pricing_service.resolve(ctx)
    assert a == b
    assert a.fingerprint == b.fingerprint
    assert a.pricing_context_hash == b.pricing_context_hash


@pytest.mark.django_db
def test_fingerprint_changes_with_profile_version(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    ts = timezone.now()
    q1 = pricing_service.resolve(_ctx(profile=family_profile, ts=ts))
    family_profile.discount_percent = Decimal("12.00")
    family_profile.save()
    family_profile.refresh_from_db()
    q2 = pricing_service.resolve(_ctx(profile=family_profile, ts=ts))
    assert q1.pricing_profile_version != q2.pricing_profile_version
    assert q1.fingerprint != q2.fingerprint
    assert q1.pricing_context_hash != q2.pricing_context_hash


@pytest.mark.django_db
def test_fingerprint_changes_with_list_price(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    ts = timezone.now()
    q1 = pricing_service.resolve(
        _ctx(profile=family_profile, ts=ts, list_price="56.00")
    )
    q2 = pricing_service.resolve(
        _ctx(profile=family_profile, ts=ts, list_price="60.00")
    )
    assert q1.fingerprint != q2.fingerprint


@pytest.mark.django_db
def test_quote_roundtrip_order_snapshot(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    ts = timezone.now()
    quote = pricing_service.resolve(_ctx(profile=family_profile, ts=ts))
    snap = quote_to_order_snapshot_kwargs(quote)
    restored = quote_from_order(type("O", (), snap)())
    assert restored == quote
    assert restored.fingerprint == quote.fingerprint


@pytest.mark.django_db
def test_quote_roundtrip_persisted_order(settings, family_profile, account, db):
    """Quote → order columns → restore equals original (DB-backed)."""
    settings.PRICING_PROFILES_ENABLED = True
    from apps.catalog.models import Location, Package

    loc = Location.objects.create(
        slug="hr",
        title="Croatia",
        country_code="HR",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    pkg = Package.objects.create(
        external_id="pkg-test-1",
        title="1GB",
        operator_title="Op",
        location=loc,
        country_code="HR",
        data_allowance="1GB",
        validity_days=7,
        price_usd=Decimal("56.00"),
        net_price_usd=Decimal("50.00"),
        synced_at=timezone.now(),
    )
    ts = timezone.now()
    quote = pricing_service.resolve(_ctx(profile=family_profile, ts=ts))
    order = Order.objects.create(
        account=account,
        package=pkg,
        status=Order.Status.DRAFT,
        **quote_to_order_snapshot_kwargs(quote),
    )
    restored = quote_from_order(order)
    assert restored == quote
    assert order.snapshot_schema_version == SNAPSHOT_SCHEMA_VERSION
    assert order.list_price_usd == Decimal("56.00")
    assert order.retail_price_usd == quote.customer_price


@pytest.mark.django_db
def test_topup_snapshot_kwargs_amount(settings, family_profile):
    settings.PRICING_PROFILES_ENABLED = True
    quote = pricing_service.resolve(_ctx(profile=family_profile))
    snap = quote_to_topup_snapshot_kwargs(quote)
    assert snap["amount"] == quote.customer_price
    assert snap["list_price_usd"] == quote.list_price


def test_no_quantize_outside_money_round_in_pricing_and_spend():
    """No Decimal.quantize in pricing/orders/esims except money.py."""
    offenders: list[str] = []
    roots = [
        SRC_ROOT / "apps" / "pricing",
        SRC_ROOT / "apps" / "orders",
        SRC_ROOT / "apps" / "esims",
    ]
    for root in roots:
        for path in root.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            if path.name == "money.py" and path.parent.name == "pricing":
                continue
            text = path.read_text(encoding="utf-8")
            if ".quantize(" in text:
                offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"quantize outside money_round: {offenders}"


def test_order_service_resolve_only_via_charge_helper():
    """PR3: OrderService uses charge helper once in reserve; not raw resolve."""
    path = SRC_ROOT / "apps" / "orders" / "services" / "order_service.py"
    text = path.read_text(encoding="utf-8")
    assert "resolve_package_charge" in text
    assert "pricing_service.resolve" not in text
    assert "amount = package.price_usd" not in text
