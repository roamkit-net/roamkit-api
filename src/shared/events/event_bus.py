"""In-process domain event bus (Celery handlers added in later phases)."""

from collections import defaultdict
from collections.abc import Callable

from shared.events.events import DomainEvent

Handler = Callable[[DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type[DomainEvent], handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        for handler in self._handlers[type(event)]:
            handler(event)


event_bus = EventBus()
