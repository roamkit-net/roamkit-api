"""Tests for AutoTopupService (design lock PR3 DoD matrix)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy, Topup
from apps.esims.services.auto_topup_service import (
    AutoTopupService,
    canonical_utc_stamp,
)
from apps.orders.exceptions import SpendInProgressError
from apps.orders.models import Order
from shared.events.esim_events import (
    AutoTopupConfigurationChanged,
    AutoTopupPausedFunds,
    AutoTopupPolicyCreated,
    AutoTopupPolicyUpdated,
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
        account=user.billing_account,
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
        "package_id": "topup-1gb",
        "enabled": True,
        "status": EsimAutoTopupPolicy.Status.ACTIVE,
        "expiry_enabled": False,
        "usage_mode": EsimAutoTopupPolicy.UsageMode.ZERO,
        "renew_mode": EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        "reason": "",
        "threshold_mb": None,
        "remaining_count": None,
        "active_until": None,
        "cooldown_until": None,
    }
    defaults.update(kwargs)
    policy, _created = EsimAutoTopupPolicy.objects.update_or_create(
        esim=esim,
        defaults=defaults,
    )
    return policy


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
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
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
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
)
def test_event_publish_failure_does_not_undo_success(
    user: User, esim: Esim, monkeypatch
) -> None:
    _fund(user)
    policy = _policy(user, esim)

    def _boom(event) -> None:
        if type(event).__name__ == "AutoTopupSucceeded":
            raise RuntimeError("handler exploded")
        real_publish(event)

    real_publish = event_bus.publish
    monkeypatch.setattr(event_bus, "publish", _boom)
    service = AutoTopupService(FakeTopupProvider())
    assert service.evaluate_one(policy.pk) == "success"
    policy.refresh_from_db()
    assert policy.cooldown_until is not None
    assert Topup.objects.count() == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, AUTO_TOPUP_ENABLED=False)
def test_evaluate_due_disabled_when_flag_off(user: User, esim: Esim) -> None:
    _fund(user)
    _policy(user, esim)
    stats = AutoTopupService(FakeTopupProvider()).evaluate_due()
    assert stats == {"disabled": 1}
    assert Topup.objects.count() == 0

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


_SETTINGS_V2 = dict(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
    AUTO_TOPUP_MINIMUM_AGE_SECONDS=0,
    AUTO_TOPUP_USAGE_MAX_AGE_SECONDS=600,
    AUTO_TOPUP_COOLDOWN_SECONDS=900,
)


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_combo_same_beat_expiry_wins_no_pending_usage(user: User, esim: Esim) -> None:
    """OR + precedence: both met → one buy with expiry; cooldown blocks usage."""
    _fund(user)
    expired_at = timezone.now() - timedelta(minutes=5)
    esim.usage_remaining_mb = 100
    esim.usage_expired_at = expired_at
    esim.usage_status = "EXPIRED"
    esim.status = Esim.Status.EXPIRED
    esim.save(
        update_fields=[
            "usage_remaining_mb",
            "usage_expired_at",
            "usage_status",
            "status",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=500,
    )
    provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=100,
            status="EXPIRED",
            expired_at=expired_at.isoformat(),
        )
    )
    metric_calls: list[tuple[str, dict]] = []

    def _capture_incr(name: str, value: int = 1, **tags: str) -> None:
        metric_calls.append((name, tags))

    service = AutoTopupService(provider)
    with patch("apps.esims.services.auto_topup_service.metrics.incr", _capture_incr):
        assert service.evaluate_one(policy.pk) == "success"

    assert Topup.objects.count() == 1
    policy.refresh_from_db()
    assert policy.last_idempotency_key is not None
    assert ":expiry:" in policy.last_idempotency_key
    assert any(
        name == "auto_topup_trigger_reason_total" and tags.get("reason") == "expiry"
        for name, tags in metric_calls
    )

    # Still in cooldown + usage still met → no second buy, no pending queue.
    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 1


def test_canonical_utc_stamp_is_one_format_for_the_same_instant() -> None:
    from datetime import timezone as dt_timezone

    naive = datetime(2026, 8, 12, 8, 50, 1, 123456)
    utc = datetime(2026, 8, 12, 8, 50, 1, 987654, tzinfo=UTC)
    offset = datetime(2026, 8, 12, 10, 50, 1, 1, tzinfo=dt_timezone(timedelta(hours=2)))
    assert canonical_utc_stamp(naive) == "2026-08-12T08:50:01+00:00"
    assert canonical_utc_stamp(utc) == "2026-08-12T08:50:01+00:00"
    assert canonical_utc_stamp(offset) == "2026-08-12T08:50:01+00:00"


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_expiry_skips_when_usage_expired_at_is_null(user: User, esim: Esim) -> None:
    _fund(user)
    esim.status = Esim.Status.EXPIRED
    esim.usage_status = "EXPIRED"
    esim.usage_expired_at = None
    esim.usage_remaining_mb = 1024
    esim.save(
        update_fields=[
            "status",
            "usage_status",
            "usage_expired_at",
            "usage_remaining_mb",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
    )
    provider = FakeTopupProvider(
        usage=_usage(remaining_mb=1024, status="EXPIRED", expired_at=None)
    )
    assert AutoTopupService(provider).evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 0
    assert provider.submit_calls == []
    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.reason == ""


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_sticky_expired_status_does_not_fire_after_topup_window(
    user: User, esim: Esim
) -> None:
    """ADR 014 leaves Esim.status expired; new usage_expired_at is the clock."""
    _fund(user)
    future_expiry = timezone.now() + timedelta(days=7)
    esim.status = Esim.Status.EXPIRED
    esim.usage_status = "NOT_ACTIVE"
    esim.usage_remaining_mb = 1024
    esim.usage_total_mb = 1024
    esim.usage_expired_at = future_expiry
    esim.usage_synced_at = timezone.now()
    esim.save(
        update_fields=[
            "status",
            "usage_status",
            "usage_remaining_mb",
            "usage_total_mb",
            "usage_expired_at",
            "usage_synced_at",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=100,
    )
    provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=1024,
            total_mb=1024,
            status="NOT_ACTIVE",
            expired_at=future_expiry.isoformat(),
        )
    )

    assert AutoTopupService(provider).evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 0
    assert provider.submit_calls == []


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_same_expiry_episode_skips_after_cooldown_without_repurchase(
    user: User, esim: Esim
) -> None:
    _fund(user)
    expired_at = timezone.now() - timedelta(minutes=5)
    esim.usage_remaining_mb = 100
    esim.usage_expired_at = expired_at
    esim.usage_status = "EXPIRED"
    esim.status = Esim.Status.EXPIRED
    esim.save(
        update_fields=[
            "usage_remaining_mb",
            "usage_expired_at",
            "usage_status",
            "status",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
    )
    provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=100,
            status="EXPIRED",
            expired_at=expired_at.isoformat(),
        )
    )
    service = AutoTopupService(provider)
    assert service.evaluate_one(policy.pk) == "success"
    assert Topup.objects.count() == 1
    assert len(provider.submit_calls) == 1

    policy.refresh_from_db()
    first_key = policy.last_idempotency_key
    first_triggered = policy.last_triggered_at
    policy.cooldown_until = None
    policy.save(update_fields=["cooldown_until"])

    assert service.evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 1
    assert len(provider.submit_calls) == 1
    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.reason == ""
    assert policy.last_idempotency_key == first_key
    assert policy.last_triggered_at == first_triggered


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_legacy_expiry_key_format_still_skips_same_episode(
    user: User, esim: Esim
) -> None:
    _fund(user)
    expired_at = datetime(2026, 8, 12, 8, 50, 1, 123456, tzinfo=UTC)
    esim.usage_remaining_mb = 100
    esim.usage_expired_at = expired_at
    esim.usage_status = "EXPIRED"
    esim.status = Esim.Status.EXPIRED
    esim.usage_synced_at = timezone.now()
    esim.save(
        update_fields=[
            "usage_remaining_mb",
            "usage_expired_at",
            "usage_status",
            "status",
            "usage_synced_at",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
    )
    policy.last_idempotency_key = (
        f"auto-topup:{policy.pk}:expiry:{expired_at.isoformat()}"
    )
    policy.save(update_fields=["last_idempotency_key"])
    provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=100,
            status="EXPIRED",
            expired_at=expired_at.isoformat(),
        )
    )
    assert AutoTopupService(provider).evaluate_one(policy.pk) == "skipped"
    assert Topup.objects.count() == 0
    assert provider.submit_calls == []


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_new_usage_expired_at_is_a_new_expiry_episode(user: User, esim: Esim) -> None:
    _fund(user)
    first_expired_at = timezone.now() - timedelta(days=2)
    esim.usage_remaining_mb = 100
    esim.usage_expired_at = first_expired_at
    esim.usage_status = "EXPIRED"
    esim.status = Esim.Status.EXPIRED
    esim.save(
        update_fields=[
            "usage_remaining_mb",
            "usage_expired_at",
            "usage_status",
            "status",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
    )
    first_provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=100,
            status="EXPIRED",
            expired_at=first_expired_at.isoformat(),
        )
    )
    assert AutoTopupService(first_provider).evaluate_one(policy.pk) == "success"
    assert Topup.objects.count() == 1

    policy.refresh_from_db()
    policy.cooldown_until = None
    policy.save(update_fields=["cooldown_until"])
    second_expired_at = timezone.now() - timedelta(minutes=5)
    esim.usage_expired_at = second_expired_at
    esim.usage_synced_at = timezone.now()
    esim.save(update_fields=["usage_expired_at", "usage_synced_at"])
    second_provider = FakeTopupProvider(
        usage=_usage(
            remaining_mb=100,
            status="EXPIRED",
            expired_at=second_expired_at.isoformat(),
        )
    )
    assert AutoTopupService(second_provider).evaluate_one(policy.pk) == "success"
    assert Topup.objects.count() == 2
    assert len(second_provider.submit_calls) == 1
    policy.refresh_from_db()
    assert ":expiry:" in (policy.last_idempotency_key or "")
    assert canonical_utc_stamp(second_expired_at) in policy.last_idempotency_key


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_after_cooldown_usage_can_fire_from_fresh_state(user: User, esim: Esim) -> None:
    _fund(user)
    expired_at = timezone.now() - timedelta(minutes=5)
    esim.usage_remaining_mb = 100
    esim.usage_expired_at = expired_at
    esim.usage_status = "EXPIRED"
    esim.status = Esim.Status.EXPIRED
    esim.save(
        update_fields=[
            "usage_remaining_mb",
            "usage_expired_at",
            "usage_status",
            "status",
        ]
    )
    policy = _policy(
        user,
        esim,
        expiry_enabled=True,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=500,
    )
    service = AutoTopupService(
        FakeTopupProvider(
            usage=_usage(
                remaining_mb=100,
                status="EXPIRED",
                expired_at=expired_at.isoformat(),
            )
        )
    )
    assert service.evaluate_one(policy.pk) == "success"

    # Clear expiry; keep usage below threshold; clear cooldown.
    policy.refresh_from_db()
    policy.cooldown_until = None
    policy.save(update_fields=["cooldown_until"])
    esim.refresh_from_db()
    esim.status = Esim.Status.ACTIVATED
    esim.usage_status = "ACTIVE"
    esim.usage_expired_at = None
    esim.usage_remaining_mb = 100
    esim.usage_synced_at = timezone.now()
    esim.save(
        update_fields=[
            "status",
            "usage_status",
            "usage_expired_at",
            "usage_remaining_mb",
            "usage_synced_at",
        ]
    )
    provider = FakeTopupProvider(usage=_usage(remaining_mb=100, status="ACTIVE"))
    service2 = AutoTopupService(provider)
    metric_calls: list[tuple[str, dict]] = []

    def _capture_incr(name: str, value: int = 1, **tags: str) -> None:
        metric_calls.append((name, tags))

    with patch("apps.esims.services.auto_topup_service.metrics.incr", _capture_incr):
        assert service2.evaluate_one(policy.pk) == "success"

    assert Topup.objects.count() == 2
    policy.refresh_from_db()
    assert ":usage_threshold:" in (policy.last_idempotency_key or "")
    assert any(
        name == "auto_topup_trigger_reason_total"
        and tags.get("reason") == "usage_threshold"
        for name, tags in metric_calls
    )


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_trigger_change_clears_cooldown(user: User, esim: Esim) -> None:
    _fund(user)
    service = AutoTopupService(FakeTopupProvider())
    policy = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=500,
        remaining_count=None,
        enabled=True,
        expected_version=None,
    )
    policy.cooldown_until = timezone.now() + timedelta(hours=1)
    policy.save(update_fields=["cooldown_until"])
    cooldown_before = policy.cooldown_until

    captured: list = []
    event_bus.subscribe(AutoTopupConfigurationChanged, captured.append)
    updated = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=200,
        remaining_count=None,
        enabled=True,
        expected_version=policy.version,
    )
    assert updated.cooldown_until is None
    assert updated.threshold_mb == 200
    assert cooldown_before is not None
    assert len(captured) == 1
    assert captured[0].before_threshold_mb == 500
    assert captured[0].after_threshold_mb == 200


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_package_only_keeps_cooldown(user: User, esim: Esim) -> None:
    _fund(user)
    extra = TopupPackage(
        external_id="topup-2gb",
        title="2 GB Top-up",
        data_allowance="2 GB",
        validity_days=7,
        price_usd=Decimal("8.00"),
        net_price_usd=Decimal("7.00"),
        is_unlimited=False,
        plan_type="topup",
    )
    provider = FakeTopupProvider(
        topups=[
            TopupPackage(
                external_id="topup-1gb",
                title="1 GB Top-up",
                data_allowance="1 GB",
                validity_days=7,
                price_usd=Decimal("5.00"),
                net_price_usd=Decimal("4.50"),
                is_unlimited=False,
                plan_type="topup",
            ),
            extra,
        ]
    )
    service = AutoTopupService(provider)
    policy = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=None,
        remaining_count=None,
        enabled=True,
        expected_version=None,
    )
    cooldown = timezone.now() + timedelta(hours=1)
    policy.cooldown_until = cooldown
    policy.save(update_fields=["cooldown_until"])

    captured: list = []
    event_bus.subscribe(AutoTopupConfigurationChanged, captured.append)
    updated = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-2gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=None,
        remaining_count=None,
        enabled=True,
        expected_version=policy.version,
    )
    assert updated.package_id == "topup-2gb"
    assert updated.cooldown_until == cooldown
    assert captured == []


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_zero_and_threshold_distinct_reasons_keys_metrics(
    user: User, esim: Esim
) -> None:
    _fund(user)
    esim.usage_remaining_mb = 0
    esim.usage_synced_at = timezone.now()
    esim.save(update_fields=["usage_remaining_mb", "usage_synced_at"])

    policy_zero = _policy(
        user,
        esim,
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        threshold_mb=None,
    )
    key_zero = AutoTopupService._idempotency_key(
        policy_zero,
        esim,
        reason=EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_ZERO,
    )
    assert ":usage_zero:" in key_zero

    policy_threshold = _policy(
        user,
        esim,
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        threshold_mb=1,
    )
    key_threshold = AutoTopupService._idempotency_key(
        policy_threshold,
        esim,
        reason=EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_THRESHOLD,
    )
    assert ":usage_threshold:" in key_threshold
    assert key_zero != key_threshold

    reason_zero = AutoTopupService._select_fire_reason(
        policy_zero, esim, now=timezone.now()
    )
    reason_threshold = AutoTopupService._select_fire_reason(
        policy_threshold, esim, now=timezone.now()
    )
    assert reason_zero == EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_ZERO
    assert reason_threshold == EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_THRESHOLD

    metric_calls: list[tuple[str, dict]] = []

    def _capture_incr(name: str, value: int = 1, **tags: str) -> None:
        metric_calls.append((name, tags))

    with patch("apps.esims.services.auto_topup_service.metrics.incr", _capture_incr):
        assert (
            AutoTopupService(
                FakeTopupProvider(usage=_usage(remaining_mb=0))
            ).evaluate_one(policy_threshold.pk)
            == "success"
        )
    assert any(
        name == "auto_topup_trigger_reason_total"
        and tags.get("reason") == "usage_threshold"
        for name, tags in metric_calls
    )


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_rejects_spend_in_progress(user: User, esim: Esim) -> None:
    Topup.objects.create(
        account=user.billing_account,
        esim=esim,
        package_external_id="topup-1gb",
        amount=Decimal("5.00"),
        status=Topup.Status.FULFILLING,
        idempotency_key="inflight-auto-topup",
    )
    service = AutoTopupService(FakeTopupProvider())
    with pytest.raises(SpendInProgressError):
        service.upsert_policy(
            esim=esim,
            account=user.billing_account,
            package_id="topup-1gb",
            expiry_enabled=True,
            usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
            renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
            threshold_mb=None,
            remaining_count=None,
            enabled=True,
            expected_version=None,
        )


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_active_until_null_still_buys(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(user, esim, active_until=None)
    provider = FakeTopupProvider()
    assert AutoTopupService(provider).evaluate_one(policy.pk) == "success"
    assert len(provider.submit_calls) == 1


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_active_until_future_still_buys(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        active_until=timezone.now() + timedelta(days=7),
    )
    provider = FakeTopupProvider()
    assert AutoTopupService(provider).evaluate_one(policy.pk) == "success"
    assert len(provider.submit_calls) == 1


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_active_until_reached_pauses_schedule_ended(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        active_until=timezone.now() - timedelta(minutes=1),
    )
    provider = FakeTopupProvider()
    metric_calls: list[tuple[str, dict]] = []
    captured: list = []
    event_bus.subscribe(AutoTopupPolicyUpdated, captured.append)

    def _capture_incr(name: str, value: int = 1, **tags: str) -> None:
        metric_calls.append((name, tags))

    with patch("apps.esims.services.auto_topup_service.metrics.incr", _capture_incr):
        assert AutoTopupService(provider).evaluate_one(policy.pk) == "paused"

    policy.refresh_from_db()
    assert policy.status == EsimAutoTopupPolicy.Status.PAUSED
    assert policy.reason == EsimAutoTopupPolicy.Reason.SCHEDULE_ENDED
    assert provider.submit_calls == []
    assert any(
        name == "auto_topup_paused_total" and tags.get("reason") == "schedule_ended"
        for name, tags in metric_calls
    )
    assert any(
        isinstance(e, AutoTopupPolicyUpdated) and e.reason == "schedule_ended"
        for e in captured
    )


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_after_schedule_ended_stays_paused(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        active_until=timezone.now() - timedelta(minutes=1),
    )
    service = AutoTopupService(FakeTopupProvider())
    assert service.evaluate_one(policy.pk) == "paused"
    assert service.evaluate_one(policy.pk) == "skipped"
    stats = service.evaluate_due()
    assert stats.get("success", 0) == 0


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_fixed_count_wins_before_active_until(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        renew_mode=EsimAutoTopupPolicy.RenewMode.FIXED_COUNT,
        remaining_count=1,
        active_until=timezone.now() + timedelta(days=30),
    )
    assert AutoTopupService(FakeTopupProvider()).evaluate_one(policy.pk) == "success"
    policy.refresh_from_db()
    assert policy.reason == EsimAutoTopupPolicy.Reason.COUNT_EXHAUSTED
    assert policy.status == EsimAutoTopupPolicy.Status.PAUSED


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_active_until_wins_before_fixed_count(user: User, esim: Esim) -> None:
    _fund(user)
    policy = _policy(
        user,
        esim,
        renew_mode=EsimAutoTopupPolicy.RenewMode.FIXED_COUNT,
        remaining_count=5,
        active_until=timezone.now() - timedelta(seconds=1),
    )
    provider = FakeTopupProvider()
    assert AutoTopupService(provider).evaluate_one(policy.pk) == "paused"
    policy.refresh_from_db()
    assert policy.reason == EsimAutoTopupPolicy.Reason.SCHEDULE_ENDED
    assert policy.remaining_count == 5
    assert provider.submit_calls == []


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_active_until_only_keeps_cooldown(user: User, esim: Esim) -> None:
    _fund(user)
    service = AutoTopupService(FakeTopupProvider())
    policy = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=None,
        remaining_count=None,
        active_until=None,
        enabled=True,
        expected_version=None,
    )
    cooldown = timezone.now() + timedelta(hours=1)
    policy.cooldown_until = cooldown
    policy.save(update_fields=["cooldown_until"])

    bound = timezone.now() + timedelta(days=14)
    captured: list = []
    event_bus.subscribe(AutoTopupConfigurationChanged, captured.append)
    updated = service.upsert_policy(
        esim=esim,
        account=user.billing_account,
        package_id="topup-1gb",
        expiry_enabled=False,
        usage_mode=EsimAutoTopupPolicy.UsageMode.ZERO,
        renew_mode=EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        threshold_mb=None,
        remaining_count=None,
        active_until=bound,
        enabled=True,
        expected_version=policy.version,
    )
    assert updated.active_until == bound
    assert updated.cooldown_until == cooldown
    assert captured == []


def _upsert(
    service: AutoTopupService,
    user: User,
    esim: Esim,
    *,
    expected_version: int | None,
    **overrides,
):
    kwargs = {
        "esim": esim,
        "account": user.billing_account,
        "package_id": "topup-1gb",
        "expiry_enabled": False,
        "usage_mode": EsimAutoTopupPolicy.UsageMode.ZERO,
        "renew_mode": EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        "threshold_mb": None,
        "remaining_count": None,
        "enabled": True,
        "expected_version": expected_version,
    }
    kwargs.update(overrides)
    return service.upsert_policy(**kwargs)


def test_is_manual_funds_resume_only_that_transition() -> None:
    after = SimpleNamespace(
        enabled=True,
        status=EsimAutoTopupPolicy.Status.ACTIVE,
        reason="",
    )
    paused_funds = AutoTopupService._is_manual_funds_resume(
        EsimAutoTopupPolicy.Status.PAUSED,
        EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS,
        after,
    )
    assert paused_funds is True
    assert (
        AutoTopupService._is_manual_funds_resume(
            EsimAutoTopupPolicy.Status.ACTIVE, "", after
        )
        is False
    )
    assert (
        AutoTopupService._is_manual_funds_resume(
            EsimAutoTopupPolicy.Status.PAUSED,
            EsimAutoTopupPolicy.Reason.PACKAGE_UNAVAILABLE,
            after,
        )
        is False
    )
    assert (
        AutoTopupService._is_manual_funds_resume(
            EsimAutoTopupPolicy.Status.PAUSED,
            EsimAutoTopupPolicy.Reason.MANUAL_PAUSE,
            after,
        )
        is False
    )
    assert (
        AutoTopupService._is_manual_funds_resume(
            EsimAutoTopupPolicy.Status.PAUSED,
            EsimAutoTopupPolicy.Reason.SCHEDULE_ENDED,
            after,
        )
        is False
    )
    disabled = SimpleNamespace(
        enabled=False,
        status=EsimAutoTopupPolicy.Status.DISABLED,
        reason=EsimAutoTopupPolicy.Reason.MANUAL_PAUSE,
    )
    assert (
        AutoTopupService._is_manual_funds_resume(
            EsimAutoTopupPolicy.Status.PAUSED,
            EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS,
            disabled,
        )
        is False
    )


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_funds_resume_enqueues_evaluate_once(user: User, esim: Esim) -> None:
    service = AutoTopupService(FakeTopupProvider())
    policy = _upsert(service, user, esim, expected_version=None)
    policy.status = EsimAutoTopupPolicy.Status.PAUSED
    policy.reason = EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
    policy.save(update_fields=["status", "reason"])

    with (
        patch(
            "apps.esims.services.auto_topup_service.transaction.on_commit",
            side_effect=lambda fn: fn(),
        ),
        patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
    ):
        updated = _upsert(service, user, esim, expected_version=policy.version)

    assert updated.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert updated.reason == ""
    delay.assert_called_once_with(str(updated.pk))


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_already_active_does_not_enqueue(user: User, esim: Esim) -> None:
    service = AutoTopupService(FakeTopupProvider())
    policy = _upsert(service, user, esim, expected_version=None)

    with (
        patch(
            "apps.esims.services.auto_topup_service.transaction.on_commit",
            side_effect=lambda fn: fn(),
        ),
        patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
    ):
        _upsert(
            service,
            user,
            esim,
            expected_version=policy.version,
            threshold_mb=None,
            expiry_enabled=True,
            usage_mode=EsimAutoTopupPolicy.UsageMode.DISABLED,
        )

    delay.assert_not_called()


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_package_change_while_active_does_not_enqueue(
    user: User, esim: Esim
) -> None:
    extra = TopupPackage(
        external_id="topup-2gb",
        title="2 GB Top-up",
        data_allowance="2 GB",
        validity_days=7,
        price_usd=Decimal("8.00"),
        net_price_usd=Decimal("7.00"),
        is_unlimited=False,
        plan_type="topup",
    )
    provider = FakeTopupProvider(
        topups=[
            TopupPackage(
                external_id="topup-1gb",
                title="1 GB Top-up",
                data_allowance="1 GB",
                validity_days=7,
                price_usd=Decimal("5.00"),
                net_price_usd=Decimal("4.50"),
                is_unlimited=False,
                plan_type="topup",
            ),
            extra,
        ]
    )
    service = AutoTopupService(provider)
    policy = _upsert(service, user, esim, expected_version=None)

    with (
        patch(
            "apps.esims.services.auto_topup_service.transaction.on_commit",
            side_effect=lambda fn: fn(),
        ),
        patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
    ):
        _upsert(
            service,
            user,
            esim,
            expected_version=policy.version,
            package_id="topup-2gb",
        )

    delay.assert_not_called()


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_other_pause_reasons_do_not_enqueue(user: User, esim: Esim) -> None:
    service = AutoTopupService(FakeTopupProvider())
    policy = _upsert(service, user, esim, expected_version=None)
    for reason in (
        EsimAutoTopupPolicy.Reason.PACKAGE_UNAVAILABLE,
        EsimAutoTopupPolicy.Reason.MANUAL_PAUSE,
        EsimAutoTopupPolicy.Reason.SCHEDULE_ENDED,
    ):
        policy.status = EsimAutoTopupPolicy.Status.PAUSED
        policy.reason = reason
        policy.save(update_fields=["status", "reason"])
        with (
            patch(
                "apps.esims.services.auto_topup_service.transaction.on_commit",
                side_effect=lambda fn: fn(),
            ),
            patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
        ):
            policy = _upsert(service, user, esim, expected_version=policy.version)
        delay.assert_not_called()


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_create_does_not_enqueue(user: User, esim: Esim) -> None:
    service = AutoTopupService(FakeTopupProvider())
    with (
        patch(
            "apps.esims.services.auto_topup_service.transaction.on_commit",
            side_effect=lambda fn: fn(),
        ),
        patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
    ):
        _upsert(service, user, esim, expected_version=None)
    delay.assert_not_called()


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_upsert_version_conflict_does_not_enqueue(user: User, esim: Esim) -> None:
    service = AutoTopupService(FakeTopupProvider())
    policy = _upsert(service, user, esim, expected_version=None)
    policy.status = EsimAutoTopupPolicy.Status.PAUSED
    policy.reason = EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
    policy.save(update_fields=["status", "reason"])

    with (
        patch(
            "apps.esims.services.auto_topup_service.transaction.on_commit",
            side_effect=lambda fn: fn(),
        ),
        patch("apps.esims.tasks.evaluate_auto_topup_policy.delay") as delay,
        pytest.raises(LookupError, match="version_conflict"),
    ):
        _upsert(service, user, esim, expected_version=policy.version + 9)
    delay.assert_not_called()


@pytest.mark.django_db
@override_settings(**_SETTINGS_V2)
def test_evaluate_auto_topup_policy_task_skips_when_disabled(
    user: User, esim: Esim
) -> None:
    from apps.esims.tasks import evaluate_auto_topup_policy

    policy = _policy(user, esim)
    with override_settings(AUTO_TOPUP_ENABLED=False, BILLING_ENABLED=True):
        assert evaluate_auto_topup_policy(str(policy.pk)) == "skipped"
