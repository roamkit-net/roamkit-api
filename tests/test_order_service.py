"""Tests for OrderService and sandbox fulfillment."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.orders.services.order_service import OrderService
from shared.events.event_bus import event_bus
from shared.events.order_events import AiraloOrderCreated
from shared.providers.esim import OrderedSimDTO, OrderResult


class FakeOrderProvider:
    def __init__(
        self, result: OrderResult | None = None, *, fail: bool = False
    ) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls.append((package_id, customer_ref))
        if self.fail:
            raise RuntimeError("provider unavailable")
        assert self.result is not None
        return self.result


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="buyer@example.com", password="secret123")


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def order_result() -> OrderResult:
    return OrderResult(
        external_order_id="9666",
        code="20230227-009666",
        package_id="pkg-us-1gb-7d",
        customer_ref="sandbox:buyer@example.com",
        currency="USD",
        price_usd=Decimal("9.50"),
        manual_installation="<p>Manual</p>",
        qrcode_installation="<p>QR</p>",
        installation_guide_url="https://sandbox.airalo.com/installation-guide",
        sims=[
            OrderedSimDTO(
                iccid="891000000000009125",
                lpa="lpa.airalo.com",
                matching_id="TEST",
                qrcode="LPA:1$lpa.airalo.com$TEST",
                qrcode_url="https://sandbox.airalo.com/qr?id=1",
                direct_apple_installation_url=(
                    "https://esimsetup.apple.com/esim_qrcode_provisioning"
                    "?carddata=LPA:1$lpa.airalo.com$TEST"
                ),
            )
        ],
    )


@pytest.mark.django_db
def test_order_service_fulfills_and_persists_esim(
    user: User, package: Package, order_result: OrderResult
) -> None:
    provider = FakeOrderProvider(order_result)
    service = OrderService(provider)

    order = service.fulfill(
        user=user,
        package=package,
        customer_ref="sandbox:buyer@example.com",
    )

    assert order.status == Order.Status.FULFILLED
    assert order.external_order_id == "9666"
    assert order.customer_ref == "sandbox:buyer@example.com"
    assert provider.calls == [("pkg-us-1gb-7d", "sandbox:buyer@example.com")]

    esim = Esim.objects.get(order=order)
    assert esim.user_id == user.pk
    assert esim.iccid == "891000000000009125"
    assert esim.lpa == "lpa.airalo.com"
    assert esim.matching_id == "TEST"
    assert esim.qrcode == "LPA:1$lpa.airalo.com$TEST"
    assert esim.manual_installation == "<p>Manual</p>"
    assert esim.qrcode_installation == "<p>QR</p>"
    assert esim.status == Esim.Status.UNUSED


@pytest.mark.django_db
def test_order_service_publishes_airalo_order_created(
    user: User, package: Package, order_result: OrderResult
) -> None:
    received: list[AiraloOrderCreated] = []
    event_bus.subscribe(AiraloOrderCreated, received.append)

    service = OrderService(FakeOrderProvider(order_result))
    order = service.fulfill(user=user, package=package, customer_ref="ref-1")

    assert len(received) == 1
    assert received[0].order_id == str(order.pk)
    assert received[0].iccid == "891000000000009125"
    assert received[0].customer_id == str(user.pk)


@pytest.mark.django_db
def test_order_service_marks_failed_on_provider_error(
    user: User, package: Package
) -> None:
    service = OrderService(FakeOrderProvider(fail=True))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.fulfill(user=user, package=package)

    order = Order.objects.get()
    assert order.status == Order.Status.FAILED
    assert Esim.objects.count() == 0


@pytest.mark.django_db
def test_order_service_defaults_customer_ref(
    user: User, package: Package, order_result: OrderResult
) -> None:
    provider = FakeOrderProvider(order_result)
    service = OrderService(provider)

    order = service.fulfill(user=user, package=package)

    assert order.customer_ref == f"rk-{order.pk}"
    assert provider.calls[0][1] == f"rk-{order.pk}"


@pytest.mark.django_db
def test_create_sandbox_esim_command(
    user: User, package: Package, order_result: OrderResult, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "apps.orders.management.commands.create_sandbox_esim.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    call_command(
        "create_sandbox_esim",
        email=user.email,
        package_id=package.external_id,
    )

    out = capsys.readouterr().out
    assert "891000000000009125" in out
    assert Order.objects.filter(user=user, status=Order.Status.FULFILLED).exists()


@pytest.mark.django_db
def test_create_sandbox_esim_unknown_user(package: Package) -> None:
    with pytest.raises(CommandError, match="not found"):
        call_command(
            "create_sandbox_esim",
            email="missing@example.com",
            package_id=package.external_id,
        )


@pytest.mark.django_db
def test_create_sandbox_esim_unknown_package(user: User) -> None:
    with pytest.raises(CommandError, match="Package"):
        call_command(
            "create_sandbox_esim",
            email=user.email,
            package_id="does-not-exist",
        )
