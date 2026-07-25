"""Domain event handlers for notifications (stubs in Faza 2 / Faza 3)."""

from __future__ import annotations

import logging

from shared.events.billing_events import CreditGranted, DepositVerified
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


def handle_deposit_verified(event: DepositVerified) -> None:
    """Stub: log deposit verification; email/webhook comes later."""
    logger.info(
        "DepositVerified deposit_id=%s account_id=%s amount=%s "
        "balance_after=%s tx_hash=%s payment_method=%s",
        event.deposit_id,
        event.account_id,
        event.amount,
        event.balance_after,
        event.tx_hash,
        event.payment_method,
    )


def handle_credit_granted(event: CreditGranted) -> None:
    """Stub: log credit grant; email/webhook comes later."""
    logger.info(
        "CreditGranted account_id=%s amount=%s balance_after=%s "
        "reference_type=%s reference_id=%s ledger_entry_id=%s",
        event.account_id,
        event.amount,
        event.balance_after,
        event.reference_type,
        event.reference_id,
        event.ledger_entry_id,
    )


def register_handlers() -> None:
    """Subscribe stub handlers once (safe under Django autoreload)."""
    global _registered
    if _registered:
        return
    event_bus.subscribe(AiraloOrderCreated, handle_airalo_order_created)
    event_bus.subscribe(DepositVerified, handle_deposit_verified)
    event_bus.subscribe(CreditGranted, handle_credit_granted)
    _registered = True
