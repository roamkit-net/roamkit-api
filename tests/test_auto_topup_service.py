"""Tests for AutoTopupService (design lock PR3 DoD matrix)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy, Topup
from apps.esims.services.auto_topup_service import AutoTopupService
from apps.orders.models import Order
from shared.events.esim_events import (
    AutoTopupPausedFunds,
    AutoTopupPolicyCreated,
    AutoTopupSucceeded,
)
from shared.events.event_bus import event_bus
from shared.providers.esim import TopupPackage, TopupResult, UsageDTO


def _usage(
    *,
    remaining_mb: int = 0,
    total_mb: int = 1024,
    status: str = "FINISHED",
    is_unlimited: bool = False,
    expired_at: str | None = None,
) -> UsageDTO:
    return UsageDTO(
        remaining_mb=remaining_mb,
        total_mb=total_mb,
        expired_at=expired_at,
        is_unlimited=is_unlimited,
        status=status,
        remaining_voice=0,
        remaining_text=0,
        total_voice=0,
        total_text=0,
    )


class FakeTopupProvider:
    def __init__(
        self,
        *,
        usage: UsageDTO | None = None,
        topups: list[TopupPackage] | None = None,
        fail_usage: bool = False,
        fail_submit: bool = False,
        empty_packages: bool = False,
    ) -> None:
        self.usage = usage or _usage()
        self.topups = (
            []
            if empty_packages
            else (
                topups
                or [
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
            )
        )
        self.fail_usage = fail_usage
        self.fail_submit = fail_submit
        self.submit_calls: list[tuple[str, str]] = []
        self.usage_calls = 0

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        self.submit_calls.append((iccid, package_id))
        if self.fail_submit:
            raise TimeoutError("provider timeout")
        return TopupResult(
            external_order_id="auto-topup-ext-1",
            code="AUTO-1",
            package_id=package_id,
            iccid=iccid,
            currency="USD",
            price_usd=Decimal("5.00"),
            customer_ref="ref-auto",
        )

    def get_usage(self, iccid: str) -> UsageDTO:
        self.usage_calls += 1
        if self.fail_usage:
            raise RuntimeError("usage unavailable")
        return self.usage


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(
        email="auto-topup-svc@example.com",
        password="secret123",
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-auto-topup-svc",
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
        external_order_id="ext-auto-topup-svc",
        customer_ref="ref-auto-topup-svc",
    )
    return Esim.objects.create(
        user=user,
        order=order,
        iccid="891000000000008888",
        status=Esim.Status.ACTIVATED,
        setup_completed_at=timezone.now() - timedelta(hours=1),
        usage_remaining_mb=0,
        usage_total_mb=1024,
        usage_status="FINISHED",
        usage_is_unlimited=False,
        usage_synced_at=timezone.now(),
    )


def _fund(user: User, amount: str = "50.00") -> None:
    credit_service.credit(
        user.billing_account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="auto-topup-fund",
        idempotency_key=f"auto-topup-fund-{user.pk}-{amount}",
    )


def _policy(user: User, esim: Esim, **kwargs) -> EsimAutoTopupPolicy:
    defaults = {
        "account": user.billing_account,
        "esim": esim,
        "package_id": "topup-1gb",
        "enabled": True,
        "status": EsimAutoTopupPolicy.Status.ACTIVE,
        "trigger_mode": EsimAutoTopupPolicy.TriggerMode.USAGE_ZERO,
        "renew_mode": EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
    }
    defaults.update(kwargs)
    return EsimAutoTopupPolicy.objects.create(**defaults)


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
    AUTO_TOPUP_USAGE_MAX_AGE_SECONDS=600,
    AUTO_TOPUP_COOLDOWN_SECONDS=900,
)
def test_usage_zero_buys_once(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim)
    provider = FakeTopupProvider()
    service = AutoTopupService(provider)

    assert service.evaluate_one(policy.pk) == "success"
    assert Topup.objects.count() == 1
    policy.refresh_from_db()
    assert policy.cooldown_until is not None
    assert policy.last_topup_id is not None

    # Cooldown blocks immediate second buy even if usage still 0.
    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 1
    assert len(provider.submit_calls) == 1


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
    AUTO_TOPUP_USAGE_MAX_AGE_SECONDS=60,
)
def test_stale_usage_refresh_fails_skips(user: User, esim: Esim) -> None:
    _fund(user)
    esim.usage_synced_at = timezone.now() - timedelta(hours=1)
    esim.save(update_fields=["usage_synced_at"])
    policy = _policy(user, esim)
    service = AutoTopupService(FakeTopupProvider(fail_usage=True))

    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 0


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_provider_timeout_retries_without_pause(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim)
    service = AutoTopupService(FakeTopupProvider(fail_submit=True))

    assert service.evaluate_one(policy.pk) == "failed"
    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.reason == ""


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_package_removed_blocks(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim, package_id="gone-pkg")
    service = AutoTopupService(FakeTopupProvider())

    assert service.evaluate_one(policy.pk) == "blocked"
    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.BLOCKED
    assert policy.reason == EsimAutoTopupPolicy.Reason.PACKAGE_UNAVAILABLE


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_insufficient_funds_pauses(user: User, esim: Esim) -> None:
    policy = _policy(user, esim)
    captured: list = []

    def _capture(event) -> None:
        captured.append(event)

    event_bus.subscribe(AutoTopupPausedFunds, _capture)
    service = AutoTopupService(FakeTopupProvider())

    assert service.evaluate_one(policy.pk) == "paused"
    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.PAUSED
    assert policy.reason == EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
    assert any(isinstance(e, AutoTopupPausedFunds) for e in captured)


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
    AUTO_TOPUP_COOLDOWN_SECONDS=900,
)
def test_fixed_count_one_then_exhausted(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        renew_mode=EsimAutoTopupPolicy.RenewMode.FIXED_COUNT,
        remaining_count=1,
    )
    service = AutoTopupService(FakeTopupProvider())

    assert service.evaluate_one(policy.pk) == "success"
    policy.refresh_from_db()
    assert policy.remaining_count == 0
    assert policy.status == EsimAutoTopupPolicy.Status.PAUSED
    assert policy.reason == EsimAutoTopupPolicy.Reason.COUNT_EXHAUSTED


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
    AUTO_TOPUP_COOLDOWN_SECONDS=900,
)
def test_threshold_fires_once_per_epoch(user: User, esim: Esim) -> None:
    _fund(user)
    esim.usage_remaining_mb = 400
    esim.save(update_fields=["usage_remaining_mb"])
    policy = _policy(
        user,
        esim,
        trigger_mode=EsimAutoTopupPolicy.TriggerMode.USAGE_THRESHOLD,
        threshold_mb=500,
    )
    provider = FakeTopupProvider(
        usage=_usage(remaining_mb=400, status="ACTIVE"),
    )
    service = AutoTopupService(provider)
    assert service.evaluate_one(policy.pk) == "success"
    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 1


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=3600,
)
def test_minimum_age_skips(user: User, esim: Esim) -> None:
    _fund(user)
    esim.setup_completed_at = timezone.now()
    esim.created_at = timezone.now()
    esim.save(update_fields=["setup_completed_at"])
    # created_at is auto_now_add — bump setup only; service uses setup_completed_at
    policy = _policy(user, esim)
    service = AutoTopupService(FakeTopupProvider())
    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 0


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="off",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_rollout_off_skips(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim)
    assert AutoTopupService(FakeTopupProvider()).evaluate_one(policy.pk) == "skipped"


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_parallel_evaluate_one_topup(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim)
    service = AutoTopupService(FakeTopupProvider())
    assert service.evaluate_one(policy.pk) == "success"
    # Same usage_synced_at → same idempotency key → replay, no second submit.
    # Clear cooldown to exercise purchase idempotency specifically.
    policy.cooldown_until = None
    policy.save(update_fields=["cooldown_until"])
    provider = FakeTopupProvider()
    service2 = AutoTopupService(provider)
    assert service2.evaluate_one(policy.pk) == "success"
    assert Topup.objects.count() == 1
    assert provider.submit_calls == []


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, AUTO_TOPUP_ENABLED=True)
def test_publish_policy_created_event(user: User, esim: Esim) -> None:
    policy = _policy(user, esim)
    captured: list = []
    event_bus.subscribe(AutoTopupPolicyCreated, captured.append)
    AutoTopupService(FakeTopupProvider()).publish_policy_created(policy, actor="user:1")
    assert len(captured) == 1
    assert captured[0].policy_id == str(policy.pk)
    assert captured[0].actor == "user:1"


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_success_publishes_event(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim)
    captured: list = []
    event_bus.subscribe(AutoTopupSucceeded, captured.append)
    AutoTopupService(FakeTopupProvider()).evaluate_one(policy.pk)
    assert len(captured) == 1
    assert captured[0].package_id == "topup-1gb"
