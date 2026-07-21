"""Order domain events."""

from dataclasses import dataclass

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class AiraloOrderCreated(DomainEvent):
    """Published after an order is fulfilled with the provider."""

    order_id: str
    iccid: str
    customer_id: str
