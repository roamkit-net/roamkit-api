"""Tests for notification event handlers."""

import logging

from apps.notifications.handlers import handle_airalo_order_created
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
