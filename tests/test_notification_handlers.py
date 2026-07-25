"""Tests for notification event handlers."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from apps.notifications.handlers import (
    handle_airalo_order_created,
    handle_credit_debited,
    handle_credit_granted,
    handle_deposit_verified,
    handle_fulfillment_refunded,
    handle_topup_completed,
)
from shared.events.billing_events import (
    CreditDebited,
    CreditGranted,
    DepositVerified,
    FulfillmentRefunded,
)
from shared.events.order_events import AiraloOrderCreated, TopupCompleted


def test_airalo_order_created_stub_logs(caplog) -> None:
    event = AiraloOrderCreated(
        order_id="42",
        iccid="891000000000009125",
        customer_id="7",
    )

    with caplog.at_level(logging.INFO):
        handle_airalo_order_created(event)

    assert "AiraloOrderCreated" in caplog.text
    assert "891000000000009125" in caplog.text
    assert "order_id=42" in caplog.text


def test_deposit_verified_stub_logs(caplog) -> None:
    verified_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    event = DepositVerified(
        deposit_id="dep-1",
        account_id="acc-1",
        amount=Decimal("10.000000"),
        balance_after=Decimal("10.000000"),
        tx_hash="0xabc",
        payment_method="wallet_connect",
        ledger_entry_id="led-1",
        verified_at=verified_at,
    )

    with caplog.at_level(logging.INFO):
        handle_deposit_verified(event)

    assert "DepositVerified" in caplog.text
    assert "dep-1" in caplog.text
    assert "0xabc" in caplog.text
    assert "led-1" in caplog.text
    assert "verified_at=" in caplog.text


def test_credit_granted_stub_logs(caplog) -> None:
    created_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    event = CreditGranted(
        account_id="acc-1",
        amount=Decimal("10.000000"),
        balance_after=Decimal("10.000000"),
        reference_type="deposit",
        reference_id="dep-1",
        ledger_entry_id="led-1",
        created_at=created_at,
    )

    with caplog.at_level(logging.INFO):
        handle_credit_granted(event)

    assert "CreditGranted" in caplog.text
    assert "led-1" in caplog.text
    assert "created_at=" in caplog.text


def test_credit_debited_stub_logs(caplog) -> None:
    created_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    event = CreditDebited(
        account_id="acc-1",
        amount=Decimal("5.000000"),
        balance_after=Decimal("5.000000"),
        reference_type="order",
        reference_id="42",
        ledger_entry_id="led-2",
        created_at=created_at,
    )

    with caplog.at_level(logging.INFO):
        handle_credit_debited(event)

    assert "CreditDebited" in caplog.text
    assert "led-2" in caplog.text


def test_fulfillment_refunded_stub_logs(caplog) -> None:
    created_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    event = FulfillmentRefunded(
        account_id="acc-1",
        amount=Decimal("5.000000"),
        balance_after=Decimal("10.000000"),
        reference_type="order",
        reference_id="42",
        ledger_entry_id="led-3",
        reason="provider_fulfillment_failed",
        created_at=created_at,
    )

    with caplog.at_level(logging.INFO):
        handle_fulfillment_refunded(event)

    assert "FulfillmentRefunded" in caplog.text
    assert "provider_fulfillment_failed" in caplog.text


def test_topup_completed_stub_logs(caplog) -> None:
    created_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    event = TopupCompleted(
        topup_id="tu-1",
        esim_id="7",
        account_id="acc-1",
        package_id="topup-1gb",
        amount=Decimal("5.000000"),
        external_order_id="ext-1",
        balance_after=Decimal("5.000000"),
        ledger_entry_id="led-4",
        created_at=created_at,
    )

    with caplog.at_level(logging.INFO):
        handle_topup_completed(event)

    assert "TopupCompleted" in caplog.text
    assert "tu-1" in caplog.text
