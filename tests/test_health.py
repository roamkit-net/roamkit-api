import pytest
from django.test import Client


@pytest.fixture
def client() -> Client:
    return Client()


def test_health_live_returns_ok(client: Client) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_health_ready_returns_ok_when_dependencies_available(client: Client) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["redis"]["status"] == "ok"


def test_domain_event_has_defaults() -> None:
    from shared.events.events import DomainEvent

    event = DomainEvent()
    assert event.event_id
    assert event.occurred_at


def test_event_bus_publishes_to_subscribers() -> None:
    from shared.events.event_bus import EventBus
    from shared.events.events import DomainEvent

    bus = EventBus()
    received: list[DomainEvent] = []
    bus.subscribe(DomainEvent, received.append)

    event = DomainEvent()
    bus.publish(event)

    assert received == [event]
