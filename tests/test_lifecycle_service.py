"""Unit tests for LifecycleService state machine and client events."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.exceptions import (
    InvalidLifecycleTransitionError,
    UnknownLifecycleEventTypeError,
)
from apps.esims.models import ActivationPolicy, Esim, EsimLifecycleEvent
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from shared.providers.esim import UsageDTO


@pytest.fixture
def user(db):
    return User.objects.create_user(email="life@example.com", password="x")


@pytest.fixture
def order(user):
    account = ensure_billing_account(user)
    package = Package.objects.create(
        external_id="pkg-life-1",
        title="Test",
        operator_title="Op",
        country_code="HR",
        data_allowance="1GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        synced_at=timezone.now(),
        activation_policy=ActivationPolicy.FIRST_USAGE,
    )
    return Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
    )


@pytest.fixture
def esim(user, order):
    return lifecycle_service.create_purchased(
        user=user,
        order=order,
        iccid="8900000000000000001",
        activation_policy=order.package.activation_policy,
    )


def test_create_purchased_snapshots_policy(esim, order):
    assert esim.status == Esim.Status.PURCHASED
    assert esim.activation_policy == ActivationPolicy.FIRST_USAGE
    order.package.activation_policy = ActivationPolicy.INSTALLATION
    order.package.save(update_fields=["activation_policy"])
    esim.refresh_from_db()
    assert esim.activation_policy == ActivationPolicy.FIRST_USAGE


def test_forbidden_reverse_transition(esim):
    lifecycle_service.transition(esim, Esim.Status.INSTALLED)
    lifecycle_service.transition(esim, Esim.Status.ACTIVATED)
    with pytest.raises(InvalidLifecycleTransitionError):
        lifecycle_service.transition(esim, Esim.Status.PURCHASED)


def test_unknown_provider_does_not_downgrade(esim):
    lifecycle_service.transition(esim, Esim.Status.INSTALLED)
    lifecycle_service.transition(esim, Esim.Status.ACTIVATED)
    usage = UsageDTO(
        remaining_mb=100,
        total_mb=100,
        expired_at=None,
        is_unlimited=False,
        status="UNKNOWN",
        remaining_voice=0,
        remaining_text=0,
        total_voice=0,
        total_text=0,
    )
    lifecycle_service.apply_provider_usage(esim, usage)
    esim.refresh_from_db()
    assert esim.status == Esim.Status.ACTIVATED


def test_provider_active_advances_to_activated(esim):
    usage = UsageDTO(
        remaining_mb=1024,
        total_mb=1024,
        expired_at=None,
        is_unlimited=False,
        status="ACTIVE",
        remaining_voice=0,
        remaining_text=0,
        total_voice=0,
        total_text=0,
    )
    lifecycle_service.apply_provider_usage(esim, usage)
    esim.refresh_from_db()
    assert esim.status == Esim.Status.ACTIVATED


def test_client_event_idempotent(esim):
    key = "idem-1"
    event1, created1 = lifecycle_service.record_client_event(
        esim,
        event_type="install.opened",
        idempotency_key=key,
        setup_session_id=uuid.uuid4(),
    )
    event2, created2 = lifecycle_service.record_client_event(
        esim,
        event_type="install.opened",
        idempotency_key=key,
        setup_session_id=uuid.uuid4(),
    )
    assert created1 is True
    assert created2 is False
    assert event1.pk == event2.pk
    esim.refresh_from_db()
    assert esim.status == Esim.Status.INSTALLATION_STARTED


def test_unknown_event_type_rejected(esim):
    with pytest.raises(UnknownLifecycleEventTypeError):
        lifecycle_service.record_client_event(
            esim,
            event_type="install.not_a_real_event",
            idempotency_key="x",
        )


def test_install_completed_and_setup_confirmed(esim):
    lifecycle_service.record_client_event(
        esim,
        event_type="install.completed",
        idempotency_key="done",
        resume_step=2,
    )
    esim.refresh_from_db()
    assert esim.status == Esim.Status.INSTALLED
    assert esim.setup_resume_step == 2

    lifecycle_service.record_client_event(
        esim,
        event_type="install.setup_confirmed",
        idempotency_key="confirm",
        resume_step=4,
    )
    esim.refresh_from_db()
    assert esim.setup_completed_at is not None


def _usage(
    *,
    status: str,
    remaining_mb: int = 1024,
    total_mb: int = 1024,
) -> UsageDTO:
    return UsageDTO(
        remaining_mb=remaining_mb,
        total_mb=total_mb,
        expired_at=None,
        is_unlimited=False,
        status=status,
        remaining_voice=0,
        remaining_text=0,
        total_voice=0,
        total_text=0,
    )


def _advance_to(esim: Esim, target: str) -> None:
    if target == Esim.Status.EXPIRED:
        lifecycle_service.transition(esim, Esim.Status.INSTALLED)
        lifecycle_service.transition(esim, Esim.Status.EXPIRED)
        return
    if target == Esim.Status.EXHAUSTED:
        lifecycle_service.transition(esim, Esim.Status.INSTALLED)
        lifecycle_service.transition(esim, Esim.Status.EXHAUSTED)
        return
    raise AssertionError(f"unsupported fixture target {target}")


def test_expired_active_zero_consumption_reactivates_to_activated(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    lifecycle_service.apply_provider_usage(
        esim, _usage(status="ACTIVE", remaining_mb=2048, total_mb=2048)
    )
    esim.refresh_from_db()
    assert esim.status == Esim.Status.ACTIVATED


def test_expired_active_with_consumption_reactivates_to_in_use(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    lifecycle_service.apply_provider_usage(
        esim, _usage(status="ACTIVE", remaining_mb=1926, total_mb=2048)
    )
    esim.refresh_from_db()
    assert esim.status == Esim.Status.IN_USE


def test_exhausted_active_zero_consumption_reactivates_to_activated(esim):
    _advance_to(esim, Esim.Status.EXHAUSTED)
    lifecycle_service.apply_provider_usage(
        esim, _usage(status="ACTIVE", remaining_mb=1024, total_mb=1024)
    )
    esim.refresh_from_db()
    assert esim.status == Esim.Status.ACTIVATED


def test_exhausted_active_with_consumption_reactivates_to_in_use(esim):
    _advance_to(esim, Esim.Status.EXHAUSTED)
    lifecycle_service.apply_provider_usage(
        esim, _usage(status="ACTIVE", remaining_mb=500, total_mb=1024)
    )
    esim.refresh_from_db()
    assert esim.status == Esim.Status.IN_USE


def test_expired_cannot_return_to_installed_or_purchased(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    with pytest.raises(InvalidLifecycleTransitionError):
        lifecycle_service.transition(esim, Esim.Status.INSTALLED)
    with pytest.raises(InvalidLifecycleTransitionError):
        lifecycle_service.transition(esim, Esim.Status.PURCHASED)
    esim.refresh_from_db()
    assert esim.status == Esim.Status.EXPIRED


def test_provider_expired_does_not_reactivate(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    esim.usage_status = "ACTIVE"
    esim.usage_remaining_mb = 1926
    esim.usage_total_mb = 2048
    esim.save(update_fields=["usage_status", "usage_remaining_mb", "usage_total_mb"])
    lifecycle_service.apply_provider_usage(esim, _usage(status="EXPIRED"))
    esim.refresh_from_db()
    assert esim.status == Esim.Status.EXPIRED


def test_provider_finished_does_not_reactivate_expired(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    lifecycle_service.apply_provider_usage(esim, _usage(status="FINISHED"))
    esim.refresh_from_db()
    assert esim.status == Esim.Status.EXPIRED


def test_provider_finished_does_not_reactivate_exhausted(esim):
    _advance_to(esim, Esim.Status.EXHAUSTED)
    lifecycle_service.apply_provider_usage(esim, _usage(status="FINISHED"))
    esim.refresh_from_db()
    assert esim.status == Esim.Status.EXHAUSTED


def test_provider_expired_advances_exhausted_only_to_expired(esim):
    _advance_to(esim, Esim.Status.EXHAUSTED)
    lifecycle_service.apply_provider_usage(esim, _usage(status="EXPIRED"))
    esim.refresh_from_db()
    assert esim.status == Esim.Status.EXPIRED


def test_repeated_active_sync_is_idempotent(esim):
    _advance_to(esim, Esim.Status.EXPIRED)
    usage = _usage(status="ACTIVE", remaining_mb=1926, total_mb=2048)
    lifecycle_service.apply_provider_usage(esim, usage)
    esim.refresh_from_db()
    assert esim.status == Esim.Status.IN_USE
    before = EsimLifecycleEvent.objects.filter(
        esim=esim, event_type="system.status.in_use"
    ).count()

    lifecycle_service.apply_provider_usage(esim, usage)
    esim.refresh_from_db()
    assert esim.status == Esim.Status.IN_USE
    after = EsimLifecycleEvent.objects.filter(
        esim=esim, event_type="system.status.in_use"
    ).count()
    assert after == before
