"""Tests for shared domain events."""

from shared.events.event_bus import EventBus
from shared.events.events import DomainEvent


def test_event_bus_isolated_instances() -> None:
    bus_a = EventBus()
    bus_b = EventBus()
    received: list[DomainEvent] = []
    bus_a.subscribe(DomainEvent, received.append)

    bus_b.publish(DomainEvent())

    assert received == []
