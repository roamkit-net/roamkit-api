"""Order fulfillment service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from apps.billing.services import ensure_billing_account
from apps.esims.models import Esim
from apps.orders.models import Order
from shared.events.event_bus import event_bus
from shared.events.order_events import AiraloOrderCreated

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.catalog.models import Package
    from shared.providers.esim import OrderProvider, OrderResult

logger = logging.getLogger(__name__)


class OrderService:
    """Creates local orders and fulfills them via an OrderProvider."""

    def __init__(self, provider: OrderProvider) -> None:
        self.provider = provider

    def fulfill(
        self,
        *,
        user: User,
        package: Package,
        customer_ref: str | None = None,
    ) -> Order:
        """
        Place a provider order and persist Order + Esim rows.

        Sandbox / ops path skips payment (status goes fulfilling → fulfilled).
        Phase 3 will create pending_payment orders before calling this.
        """
        order = self._create_local_order(
            user=user, package=package, customer_ref=customer_ref
        )

        try:
            result = self.provider.create_order(package.external_id, order.customer_ref)
        except Exception:
            order.status = Order.Status.FAILED
            order.save(update_fields=["status", "updated_at"])
            logger.exception("Order %s provider fulfillment failed", order.pk)
            raise

        esims = self._persist_fulfillment(order, result)
        self._publish_created_events(order, user, esims)
        return order

    def _create_local_order(
        self,
        *,
        user: User,
        package: Package,
        customer_ref: str | None,
    ) -> Order:
        with transaction.atomic():
            account = ensure_billing_account(user)
            order = Order.objects.create(
                account=account,
                package=package,
                status=Order.Status.FULFILLING,
                customer_ref=customer_ref or "",
            )
            if not order.customer_ref:
                order.customer_ref = f"rk-{order.pk}"
                order.save(update_fields=["customer_ref", "updated_at"])
        return order

    def _persist_fulfillment(self, order: Order, result: OrderResult) -> list[Esim]:
        with transaction.atomic():
            order.external_order_id = result.external_order_id
            order.status = Order.Status.FULFILLED
            order.save(update_fields=["external_order_id", "status", "updated_at"])

            esims: list[Esim] = []
            for sim in result.sims:
                esims.append(
                    Esim.objects.create(
                        user=order.account.user,
                        order=order,
                        iccid=sim.iccid,
                        lpa=sim.lpa,
                        matching_id=sim.matching_id,
                        qrcode=sim.qrcode,
                        qrcode_url=sim.qrcode_url,
                        direct_apple_installation_url=(
                            sim.direct_apple_installation_url
                        ),
                        manual_installation=result.manual_installation,
                        qrcode_installation=result.qrcode_installation,
                        installation_guide_url=result.installation_guide_url,
                        status=Esim.Status.UNUSED,
                    )
                )
        return esims

    def _publish_created_events(
        self, order: Order, user: User, esims: list[Esim]
    ) -> None:
        for esim in esims:
            event_bus.publish(
                AiraloOrderCreated(
                    order_id=str(order.pk),
                    iccid=esim.iccid,
                    customer_id=str(user.pk),
                )
            )
