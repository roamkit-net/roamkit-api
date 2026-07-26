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
from apps.esims.models import ActivationPolicy, Esim
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
