"""Order fulfillment service — debit-reserve then provider (ADR-010 / ADR-019)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.billing.exceptions import BillingDisabledError
from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service, ensure_billing_account
from apps.esims.models import Esim
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.exceptions import IdempotencyKeyRequiredError, SpendInProgressError
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.pricing.charge import resolve_package_charge
from shared.events.billing_events import (
    CreditDebited,
    CreditGranted,
    FulfillmentRefunded,
)
from shared.events.event_bus import event_bus
from shared.events.order_events import AiraloOrderCreated

if TYPE_CHECKING:

    from apps.accounts.models import User
    from apps.billing.models import Account, CreditLedgerEntry
    from apps.catalog.models import Package
    from shared.providers.esim import OrderProvider, OrderResult

logger = logging.getLogger(__name__)


class OrderService:
    """Creates local orders and fulfills them via an OrderProvider.

    Paid path (default): resolve price once → snapshot on Order → debit from
    snapshot → provider. Provider failure → compensating REFUND from snapshot
    charged amount (never re-resolve).

    Paid requests require ``idempotency_key`` (same pattern as deposits):
    retries return the prior FULFILLED/FAILED result without a second debit
    or provider call. A still-FULFILLING key raises ``SpendInProgressError``.

    ``skip_payment=True`` is for sandbox / ops only (no ledger mutation).
    """

    def __init__(self, provider: OrderProvider) -> None:
        self.provider = provider

    def fulfill(
        self,
        *,
        user: User,
        package: Package,
        customer_ref: str | None = None,
        skip_payment: bool = False,
        idempotency_key: str | None = None,
    ) -> Order:
        """Place a provider order and persist Order + Esim rows."""
        account = ensure_billing_account(user)

        if not skip_payment and not idempotency_key:
            raise IdempotencyKeyRequiredError("idempotency_key is required")

        order, debit_entry, replay = self._reserve_and_debit(
            account=account,
            package=package,
            customer_ref=customer_ref,
            skip_payment=skip_payment,
            idempotency_key=idempotency_key,
        )
        if replay:
            return order

        # Snapshot is source of truth for charge after reserve (ADR 019).
        amount = order.retail_price_usd
        if amount is None:
            raise RuntimeError("Order missing retail_price_usd snapshot after reserve")

        if debit_entry is not None:
            event_bus.publish(
                CreditDebited(
                    account_id=str(account.pk),
                    amount=amount,
                    balance_after=debit_entry.balance_after,
                    reference_type=LedgerReferenceType.ORDER,
                    reference_id=str(order.pk),
                    ledger_entry_id=str(debit_entry.pk),
                    created_at=debit_entry.created_at,
                )
            )

        try:
            result = self.provider.create_order(package.external_id, order.customer_ref)
        except Exception:
            self._compensate_failed(
                order=order,
                account=account,
                skip_payment=skip_payment,
            )
            logger.exception("Order %s provider fulfillment failed", order.pk)
            raise

        esims = self._persist_fulfillment(order, result)
        self._publish_created_events(order, user, esims)
        return order

    def _reserve_and_debit(
        self,
        *,
        account: Account,
        package: Package,
        customer_ref: str | None,
        skip_payment: bool,
        idempotency_key: str | None,
    ) -> tuple[Order, CreditLedgerEntry | None, bool]:
        with transaction.atomic():
            if idempotency_key:
                existing = (
                    Order.objects.select_for_update()
                    .filter(idempotency_key=idempotency_key)
                    .first()
                )
                if existing is not None:
                    if existing.status == Order.Status.FULFILLING:
                        raise SpendInProgressError("Order request is still in progress")
                    return existing, None, True

            # Resolve at most once per new order; snapshot before debit.
            _charge, pricing_kwargs = resolve_package_charge(
                account=account, package=package
            )
            create_kwargs = {
                **product_snapshot_kwargs(package),
                **pricing_kwargs,
            }
            # Ensure charged amount is always on the row (legacy + flag paths).
            if "retail_price_usd" not in pricing_kwargs:
                create_kwargs["retail_price_usd"] = package.price_usd
                create_kwargs.setdefault("list_price_usd", package.price_usd)

            try:
                order = Order.objects.create(
                    account=account,
                    package=package,
                    status=Order.Status.FULFILLING,
                    customer_ref=customer_ref or "",
                    idempotency_key=idempotency_key or None,
                    **create_kwargs,
                )
            except IntegrityError:
                existing = Order.objects.filter(idempotency_key=idempotency_key).first()
                if existing is None:
                    raise
                if existing.status == Order.Status.FULFILLING:
                    raise SpendInProgressError(
                        "Order request is still in progress"
                    ) from None
                return existing, None, True

            if not order.customer_ref:
                order.customer_ref = f"rk-{order.pk}"
                order.save(update_fields=["customer_ref", "updated_at"])

            if skip_payment:
                return order, None, False

            if not settings.BILLING_ENABLED:
                raise BillingDisabledError("Billing is disabled")

            amount = order.retail_price_usd
            if amount is None:
                raise RuntimeError("Order missing retail_price_usd before debit")

            entry = credit_service.debit(
                account,
                amount,
                reference_type=LedgerReferenceType.ORDER,
                reference_id=str(order.pk),
                idempotency_key=f"order-debit:{order.pk}",
            )
            return order, entry, False

    def _compensate_failed(
        self,
        *,
        order: Order,
        account: Account,
        skip_payment: bool,
    ) -> None:
        events: list[CreditGranted | FulfillmentRefunded] = []
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.status == Order.Status.FAILED:
                return
            locked.status = Order.Status.FAILED
            locked.save(update_fields=["status", "updated_at"])

            if skip_payment:
                return

            amount = locked.retail_price_usd
            if amount is None:
                raise RuntimeError("Order missing retail_price_usd for refund")

            entry = credit_service.credit(
                account,
                amount,
                reference_type=LedgerReferenceType.REFUND,
                reference_id=str(locked.pk),
                idempotency_key=f"order-refund:{locked.pk}",
            )
            events.append(
                CreditGranted(
                    account_id=str(account.pk),
                    amount=amount,
                    balance_after=entry.balance_after,
                    reference_type=LedgerReferenceType.REFUND,
                    reference_id=str(locked.pk),
                    ledger_entry_id=str(entry.pk),
                    created_at=entry.created_at,
                )
            )
            events.append(
                FulfillmentRefunded(
                    account_id=str(account.pk),
                    amount=amount,
                    balance_after=entry.balance_after,
                    reference_type=LedgerReferenceType.ORDER,
                    reference_id=str(locked.pk),
                    ledger_entry_id=str(entry.pk),
                    reason="provider_fulfillment_failed",
                    created_at=entry.created_at,
                )
            )

        for event in events:
            event_bus.publish(event)

    def _persist_fulfillment(self, order: Order, result: OrderResult) -> list[Esim]:
        with transaction.atomic():
            order.external_order_id = result.external_order_id
            order.status = Order.Status.FULFILLED
            order.save(update_fields=["external_order_id", "status", "updated_at"])

            esims: list[Esim] = []
            policy = getattr(order.package, "activation_policy", "unknown") or "unknown"
            for sim in result.sims:
                esims.append(
                    lifecycle_service.create_purchased(
                        user=order.account.user,
                        account=order.account,
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
                        activation_policy=policy,
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
