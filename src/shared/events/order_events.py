"""Order / top-up domain events."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class AiraloOrderCreated(DomainEvent):
    """Published after an order is fulfilled with the provider."""

    order_id: str
    iccid: str
    customer_id: str


@dataclass(frozen=True, kw_only=True)
class TopupCompleted(DomainEvent):
    """Published after a top-up is fulfilled with the provider."""

    event_version: int = 1
    topup_id: str
    esim_id: str
    account_id: str
    package_id: str
    amount: Decimal
    external_order_id: str
    balance_after: Decimal
    ledger_entry_id: str
    created_at: datetime
