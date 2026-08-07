"""Model tests for EsimAutoTopupPolicy (auto top-up v2 schema PR2)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.orders.models import Order


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(
        email="auto-topup-schema@example.com",
        password="secret123",
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-auto-topup-schema",
        title="1 GB - 7 Days",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("10.00"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def esim(user: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-auto-topup-schema",
        customer_ref="ref-auto-topup-schema",
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000009999",
        status=Esim.Status.ACTIVATED,
    )


@pytest.mark.django_db
def test_create_policy_until_funds_usage_zero(user: User, esim: Esim) -> None:
    policy = EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    assert isinstance(policy.pk, uuid.UUID)
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.reason == ""
    assert policy.enabled is True
    assert policy.version == 0
    assert policy.threshold_mb is None
    assert policy.remaining_count is None
    assert policy.cooldown_until is None
    assert policy.legacy_trigger_mode() == "usage_zero"


@pytest.mark.django_db
def test_create_policy_threshold_and_fixed_count(user: User, esim: Esim) -> None:
    policy = EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-3gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=500,
        renew_mode=EsimAutoTopupPolicy.RenewMode.FIXED_COUNT,
        remaining_count=3,
    )
    assert policy.threshold_mb == 500
    assert policy.remaining_count == 3
    assert policy.legacy_trigger_mode() == "usage_threshold"


@pytest.mark.django_db
def test_create_policy_expiry_only(user: User, esim: Esim) -> None:
    policy = EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-1gb",
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    assert policy.legacy_trigger_mode() == "expiry"


@pytest.mark.django_db
def test_create_policy_combo_expiry_and_threshold(user: User, esim: Esim) -> None:
    policy = EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-1gb",
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=500,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    assert policy.expiry_enabled is True
    assert policy.usage_mode == EsimAutoTopupPolicy.UsageMode.THRESHOLD


@pytest.mark.django_db
def test_one_policy_per_esim(user: User, esim: Esim) -> None:
    EsimAutoTopupPolicy.objects.create(
        account=user.billing_account,
        esim=esim,
        package_id="topup-1gb",
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        EsimAutoTopupPolicy.objects.create(
            account=user.billing_account,
            esim=esim,
            package_id="topup-2gb",
            expiry_enabled=False,
            usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
            renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        )


@pytest.mark.django_db
def test_threshold_required_for_usage_threshold(user: User, esim: Esim) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        EsimAutoTopupPolicy.objects.create(
            account=user.billing_account,
            esim=esim,
            package_id="topup-1gb",
            expiry_enabled=False,
            usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
            threshold_mb=None,
            renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        )


@pytest.mark.django_db
def test_remaining_count_required_for_fixed_count(user: User, esim: Esim) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        EsimAutoTopupPolicy.objects.create(
            account=user.billing_account,
            esim=esim,
            package_id="topup-1gb",
            expiry_enabled=False,
            usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
            renew_mode=EsimAutoTopupPolicy.RenewMode.FIXED_COUNT,
            remaining_count=None,
        )


@pytest.mark.django_db
def test_apply_legacy_trigger_mode_helpers() -> None:
    expiry_enabled, usage_mode = EsimAutoTopupPolicy.fields_from_legacy_trigger(
        "expiry"
    )
    assert expiry_enabled is True
    assert usage_mode == EsimAutoTopupPolicy.UsageMode.DISABLED
    expiry_enabled, usage_mode = EsimAutoTopupPolicy.fields_from_legacy_trigger(
        "usage_threshold"
    )
    assert expiry_enabled is False
    assert usage_mode == EsimAutoTopupPolicy.UsageMode.THRESHOLD
    expiry_enabled, usage_mode = EsimAutoTopupPolicy.fields_from_legacy_trigger(
        "usage_zero"
    )
    assert expiry_enabled is False
    assert usage_mode == EsimAutoTopupPolicy.UsageMode.ZERO


@pytest.mark.django_db
def test_trigger_mode_column_removed() -> None:
    column_names = {
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(),
            EsimAutoTopupPolicy._meta.db_table,
        )
    }
    assert "trigger_mode" not in column_names
    assert "expiry_enabled" in column_names
    assert "usage_mode" in column_names
