"""Domain event handlers for notifications (stubs in Faza 2)."""

from __future__ import annotations

import logging

from shared.events.event_bus import event_bus
from shared.events.order_events import AiraloOrderCreated

logger = logging.getLogger(__name__)

_registered = False


def handle_airalo_order_created(event: AiraloOrderCreated) -> None:
    """Stub: log order fulfillment; email/webhook comes later."""
    logger.info(
        "AiraloOrderCreated order_id=%s iccid=%s customer_id=%s",
        event.order_id,
        event.iccid,
        event.customer_id,
    )


def register_handlers() -> None:
    """Subscribe stub handlers once (safe under Django autoreload)."""
    global _registered
    if _registered:
        return
    event_bus.subscribe(AiraloOrderCreated, handle_airalo_order_created)
    _registered = True
