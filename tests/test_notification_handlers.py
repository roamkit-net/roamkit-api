"""Tests for notification event handlers."""

import logging
from decimal import Decimal

from apps.notifications.handlers import (
    handle_airalo_order_created,
    handle_credit_granted,
    handle_deposit_verified,
)
from shared.events.billing_events import CreditGranted, DepositVerified
from shared.events.order_events import AiraloOrderCreated


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
    event = DepositVerified(
        deposit_id="dep-1",
        account_id="acc-1",
        amount=Decimal("10.000000"),
        balance_after=Decimal("10.000000"),
        tx_hash="0xabc",
        payment_method="wallet_connect",
    )

    with caplog.at_level(logging.INFO):
        handle_deposit_verified(event)

    assert "DepositVerified" in caplog.text
    assert "dep-1" in caplog.text
    assert "0xabc" in caplog.text


def test_credit_granted_stub_logs(caplog) -> None:
    event = CreditGranted(
        account_id="acc-1",
        amount=Decimal("10.000000"),
        balance_after=Decimal("10.000000"),
        reference_type="deposit",
        reference_id="dep-1",
        ledger_entry_id="led-1",
    )

    with caplog.at_level(logging.INFO):
        handle_credit_granted(event)

    assert "CreditGranted" in caplog.text
    assert "led-1" in caplog.text
