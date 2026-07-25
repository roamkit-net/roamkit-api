"""Domain event handlers for notifications (stubs in Faza 2 / Faza 3)."""

from __future__ import annotations

import logging

from shared.events.billing_events import (
    CreditDebited,
    CreditGranted,
    DepositVerified,
    FulfillmentRefunded,
)
from shared.events.event_bus import event_bus
from shared.events.order_events import AiraloOrderCreated, TopupCompleted

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


def handle_topup_completed(event: TopupCompleted) -> None:
    """Stub: log top-up fulfillment; email/webhook comes later."""
    logger.info(
        "TopupCompleted topup_id=%s esim_id=%s account_id=%s "
        "package_id=%s amount=%s external_order_id=%s "
        "balance_after=%s ledger_entry_id=%s created_at=%s",
        event.topup_id,
        event.esim_id,
        event.account_id,
        event.package_id,
        event.amount,
        event.external_order_id,
        event.balance_after,
        event.ledger_entry_id,
        event.created_at,
    )


def handle_deposit_verified(event: DepositVerified) -> None:
    """Stub: log deposit verification; email/webhook comes later."""
    logger.info(
        "DepositVerified deposit_id=%s account_id=%s amount=%s "
        "balance_after=%s tx_hash=%s payment_method=%s "
        "ledger_entry_id=%s verified_at=%s",
        event.deposit_id,
        event.account_id,
        event.amount,
        event.balance_after,
        event.tx_hash,
        event.payment_method,
        event.ledger_entry_id,
        event.verified_at,
    )


def handle_credit_granted(event: CreditGranted) -> None:
    """Stub: log credit grant; email/webhook comes later."""
    logger.info(
        "CreditGranted account_id=%s amount=%s balance_after=%s "
        "reference_type=%s reference_id=%s ledger_entry_id=%s created_at=%s",
        event.account_id,
        event.amount,
        event.balance_after,
        event.reference_type,
        event.reference_id,
        event.ledger_entry_id,
        event.created_at,
    )


def handle_credit_debited(event: CreditDebited) -> None:
    """Stub: log credit debit; email/webhook comes later."""
    logger.info(
        "CreditDebited account_id=%s amount=%s balance_after=%s "
        "reference_type=%s reference_id=%s ledger_entry_id=%s created_at=%s",
        event.account_id,
        event.amount,
        event.balance_after,
        event.reference_type,
        event.reference_id,
        event.ledger_entry_id,
        event.created_at,
    )


def handle_fulfillment_refunded(event: FulfillmentRefunded) -> None:
    """Stub: log compensating refund after provider failure."""
    logger.info(
        "FulfillmentRefunded account_id=%s amount=%s balance_after=%s "
        "reference_type=%s reference_id=%s ledger_entry_id=%s "
        "reason=%s created_at=%s",
        event.account_id,
        event.amount,
        event.balance_after,
        event.reference_type,
        event.reference_id,
        event.ledger_entry_id,
        event.reason,
        event.created_at,
    )


def register_handlers() -> None:
    """Subscribe stub handlers once (safe under Django autoreload)."""
    global _registered
    if _registered:
        return
    event_bus.subscribe(AiraloOrderCreated, handle_airalo_order_created)
    event_bus.subscribe(TopupCompleted, handle_topup_completed)
    event_bus.subscribe(DepositVerified, handle_deposit_verified)
    event_bus.subscribe(CreditGranted, handle_credit_granted)
    event_bus.subscribe(CreditDebited, handle_credit_debited)
    event_bus.subscribe(FulfillmentRefunded, handle_fulfillment_refunded)
    _registered = True
